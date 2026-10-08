from contextlib import asynccontextmanager
from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import asyncio
import base64
import hashlib
import hmac
import io
import json
import mimetypes
import os
import re
import zipfile
from datetime import datetime, timezone
import pytz

from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15
from Crypto.Hash import SHA256

# Chave pública fixa do app mobile (LogScan) — verifica a assinatura RSA do
# .db de contagens exportado (par gerado em CSCollect/security/export_db_signing.py;
# a chave privada correspondente só existe no app, nunca aqui).
_EXPORT_DB_PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAzWvISBG0DElIbeXn5GoB
NXGBz0NHBde4eLm87Rf3rtc82cR8P7j/P4lVce2FGTL2HmL3ZV8WZ7YdXW3CZ9YF
5mwMZnfgZxZNbcfYM7rGyoBh8ejyGhTJU3xvBuqSY0zM2DLXBYeL81isefGIQUKw
u/JKmTtJlntjzuyyU0iPyGwuC9Txz6w688Z3xAWoYMyhHMxz2PoOco937D7BEkDO
2yuq7aRvNXB4fT1s17kfSEhftOQl5LtSrDesyZMhAmSAbjWARhD+afumzFxPoHGI
GcD2U7DIQi3Kkkly4BPwYW+7C5quNkqottp7Fxvln2rd4+5240U2vHtg7HqMOFeu
PQIDAQAB
-----END PUBLIC KEY-----"""

# Carrega .env local se existir (útil para desenvolvimento)
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# ==============================
# CONFIG BANCO (NEON)
# ==============================

DATABASE_URL = os.getenv("DATABASE_URL")  # usar variável de ambiente

if not DATABASE_URL:
    raise Exception("Defina a variável de ambiente DATABASE_URL")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
Session = sessionmaker(bind=engine)

# ==============================
# APP
# ==============================

# ==============================
# LIMPEZA AUTOMÁTICA (3 HORAS)
# ==============================

EXPIRACAO_HORAS = 3
INTERVALO_LIMPEZA_SEGUNDOS = 60 * 60  # verifica a cada 60 minutos

# O conteúdo das cargas e contagens fica no próprio Neon, na tabela
# arquivos_transferencia — e não no disco do Render. O filesystem do Render é
# efêmero: é apagado a cada redeploy/restart e, no plano free, sempre que a
# instância hiberna após ~15 min sem requisições. Com os arquivos em disco, a
# carga/contagem continuava registrada no banco mas o download dava 404 muito
# antes das 3 horas.
#
# A validade é medida pelo `criado_em` (timestamptz, preenchido pelo Postgres)
# contra `now()`, inteiramente no SQL: não depende do tipo nem do fuso das
# colunas `data_envio`, que o app mobile também grava direto no Neon.
_SQL_ARQUIVO_VALIDO = "a.criado_em >= now() - make_interval(hours => :horas)"


def _garantir_tabela_arquivos():
    """Cria a tabela de arquivos no primeiro startup após o deploy."""
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS arquivos_transferencia (
                id           BIGSERIAL    PRIMARY KEY,
                tipo         TEXT         NOT NULL,  -- 'carga' ou 'contagem'
                cnpj         TEXT         NOT NULL,
                pasta        TEXT         NOT NULL,  -- codvendedor (carga) / idcelular (contagem)
                nome_arquivo TEXT         NOT NULL,
                conteudo     BYTEA        NOT NULL,
                criado_em    TIMESTAMPTZ  NOT NULL DEFAULT now(),
                UNIQUE (tipo, cnpj, pasta, nome_arquivo)
            )
        """))


def _salvar_arquivo(db, tipo: str, cnpj: str, pasta: str, nome: str, conteudo: bytes):
    """Grava o arquivo; reenviar o mesmo nome substitui o conteúdo e renova a validade."""
    db.execute(
        text("""
            INSERT INTO arquivos_transferencia (tipo, cnpj, pasta, nome_arquivo, conteudo)
            VALUES (:tipo, :cnpj, :pasta, :nome, :conteudo)
            ON CONFLICT (tipo, cnpj, pasta, nome_arquivo)
            DO UPDATE SET conteudo = EXCLUDED.conteudo, criado_em = now()
        """),
        {"tipo": tipo, "cnpj": cnpj, "pasta": pasta, "nome": nome, "conteudo": conteudo}
    )


def _ler_arquivo(db, tipo: str, cnpj: str, pasta: str, nome: str):
    """Conteúdo do arquivo, ou None se não existir ou já tiver expirado."""
    row = db.execute(
        text(f"""
            SELECT a.conteudo FROM arquivos_transferencia a
            WHERE a.tipo = :tipo AND a.cnpj = :cnpj AND a.pasta = :pasta
              AND a.nome_arquivo = :nome AND {_SQL_ARQUIVO_VALIDO}
        """),
        {"tipo": tipo, "cnpj": cnpj, "pasta": pasta, "nome": nome, "horas": EXPIRACAO_HORAS}
    ).fetchone()
    return bytes(row[0]) if row else None


def _remover_arquivo(db, tipo: str, cnpj: str, pasta: str, nome: str) -> bool:
    """Apaga o arquivo. Retorna False se ele não existia."""
    res = db.execute(
        text("""
            DELETE FROM arquivos_transferencia
            WHERE tipo = :tipo AND cnpj = :cnpj AND pasta = :pasta AND nome_arquivo = :nome
        """),
        {"tipo": tipo, "cnpj": cnpj, "pasta": pasta, "nome": nome}
    )
    return res.rowcount > 0


def _resposta_arquivo(conteudo: bytes, nome: str) -> Response:
    media_type = mimetypes.guess_type(nome)[0] or "application/octet-stream"
    return Response(content=conteudo, media_type=media_type)


def _agora_local() -> datetime:
    """Agora no fuso de São Paulo, sem tzinfo — valor gravado em ``data_envio``.

    ``data_envio`` serve só para exibição/ordenação; a validade de 3 horas é
    controlada por ``arquivos_transferencia.criado_em``.
    """
    return datetime.now(pytz.timezone('America/Sao_Paulo')).replace(tzinfo=None)


def _fmt_data_envio(dt):
    """Formata um ``data_envio`` já com o offset de São Paulo.

    Aceita tanto o valor naive (hora de São Paulo, gravado pela API) quanto
    um aware (coluna ``timestamptz``).
    """
    if dt is None:
        return None
    tz = pytz.timezone('America/Sao_Paulo')
    dt = dt.astimezone(tz) if dt.tzinfo else tz.localize(dt)
    return dt.strftime('%Y-%m-%d %H:%M:%S%z')


def _limpar_expirados():
    """
    Apaga do banco (Neon) os arquivos com mais de EXPIRACAO_HORAS e as
    referências em `cargas`/`contagens` que ficaram sem arquivo — expirado,
    removido, ou de antes de os arquivos passarem a ficar no banco.
    Executado em background pela tarefa assíncrona.
    """
    db = Session()
    try:
        arquivos = db.execute(
            text(f"DELETE FROM arquivos_transferencia a WHERE NOT ({_SQL_ARQUIVO_VALIDO})"),
            {"horas": EXPIRACAO_HORAS}
        ).rowcount

        cargas = db.execute(text("""
            DELETE FROM cargas c
            WHERE NOT EXISTS (
                SELECT 1 FROM arquivos_transferencia a
                WHERE a.tipo = 'carga' AND a.cnpj = c.cnpj
                  AND a.pasta = c.codvendedor AND a.nome_arquivo = c.nome_arquivo
            )
        """)).rowcount

        contagens = db.execute(text("""
            DELETE FROM contagens c
            WHERE NOT EXISTS (
                SELECT 1 FROM arquivos_transferencia a
                WHERE a.tipo = 'contagem' AND a.cnpj = c.cnpj
                  AND a.pasta = c.idcelular AND a.nome_arquivo = c.nome_arquivo
            )
        """)).rowcount

        db.commit()
        if arquivos or cargas or contagens:
            print(f"[LIMPEZA] Removido(s): {arquivos} arquivo(s), "
                  f"{cargas} carga(s), {contagens} contagem(s).")
    except Exception as e:
        db.rollback()
        print(f"[LIMPEZA] Erro durante limpeza: {e}")
    finally:
        db.close()


async def _tarefa_limpeza():
    """Loop assíncrono que executa a limpeza periódica de registros expirados.

    Limpa já no startup: no plano free do Render a instância hiberna após
    ~15 min ociosa, então um loop que só limpasse depois da primeira hora
    quase nunca chegaria a rodar.
    """
    while True:
        try:
            await asyncio.to_thread(_limpar_expirados)
        except Exception as e:
            print(f"[LIMPEZA] Exceção não tratada na tarefa de limpeza: {e}")
        await asyncio.sleep(INTERVALO_LIMPEZA_SEGUNDOS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Inicia a tarefa de limpeza no startup e a cancela no shutdown."""
    try:
        _garantir_tabela_arquivos()
    except Exception as e:
        print(f"[ARQUIVOS] Falha ao criar a tabela arquivos_transferencia: {e}")
    tarefa = asyncio.create_task(_tarefa_limpeza())
    print(f"[LIMPEZA] Tarefa de limpeza iniciada (expiração: {EXPIRACAO_HORAS}h, intervalo: {INTERVALO_LIMPEZA_SEGUNDOS}s).")
    yield
    tarefa.cancel()
    try:
        await tarefa
    except asyncio.CancelledError:
        pass


app = FastAPI(lifespan=lifespan)

# R3: Rate limiting — 10 tentativas por minuto por IP nos endpoints públicos
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ==============================
# VALIDAÇÃO DE ASSINATURA .SIG
# ==============================

def validar_sig_bytes(zip_bytes: bytes, token_cliente: str) -> dict:
    """
    Valida o arquivo .sig contido no ZIP exportado pelo CSCollect.
    Retorna dict com:
        ok      : bool  — True se assinatura e hashes são válidos
        erros   : list  — lista de mensagens de erro encontradas
        payload : dict  — payload do .sig (mesmo se inválido)
    """
    erros = []
    payload = {}

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes), 'r') as zf:
            names = zf.namelist()

            # 1. Localizar o .sig
            sig_names = [n for n in names if n.endswith('.sig')]
            if not sig_names:
                return {'ok': False, 'erros': ['Arquivo .sig não encontrado no ZIP'], 'payload': {}}

            sig_content = zf.read(sig_names[0]).decode('utf-8')
            doc = json.loads(sig_content)
            payload    = doc.get('payload', {})
            assinatura = doc.get('assinatura', '')

            # 2. JSON canônico do payload
            payload_json  = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                       separators=(',', ':'))
            payload_bytes = payload_json.encode('utf-8')

            # 3. Validar HMAC (pula se serial vazio — modo offline)
            serial = payload.get('serial', '')
            if serial:
                chave = (token_cliente or '').encode('utf-8')
                expected_sig = hmac.new(chave, payload_bytes, hashlib.sha256).hexdigest()
                if not hmac.compare_digest(expected_sig, assinatura):
                    erros.append('Assinatura HMAC inválida — token não confere ou payload adulterado')
                    print(f"[validar_sig] HMAC FAIL cnpj={payload.get('cnpj')} "
                          f"key_len={len(chave)} serial_pref={serial[:6]} "
                          f"esperado={expected_sig[:12]} recebido={assinatura[:12]}")
            # se serial vazio, omite validação HMAC e verifica apenas integridade

            # 4. Helper SHA-256 de entrada do ZIP
            def _sha256_entry(name):
                h = hashlib.sha256()
                with zf.open(name) as f:
                    for chunk in iter(lambda: f.read(65536), b''):
                        h.update(chunk)
                return h.hexdigest()

            # DB (contagens)
            db_names = [n for n in names if n.endswith('.db')]
            if db_names:
                h = _sha256_entry(db_names[0])
                if h != payload.get('hash_db', ''):
                    erros.append(f'Hash DB diverge: esperado={payload.get("hash_db")} calculado={h}')

                # Assinatura RSA do .db (a mesma que o ERP VB6 verifica offline) —
                # validada aqui também como camada extra de integridade no recebimento.
                assinatura_rsa = doc.get('assinatura_rsa', '')
                if assinatura_rsa:
                    try:
                        db_bytes = zf.read(db_names[0])
                        digest = SHA256.new(db_bytes)
                        chave_pub = RSA.import_key(_EXPORT_DB_PUBLIC_KEY_PEM)
                        pkcs1_15.new(chave_pub).verify(digest, base64.b64decode(assinatura_rsa))
                    except (ValueError, TypeError):
                        erros.append('Assinatura RSA do .db inválida — arquivo pode ter sido alterado')
                    except Exception as e:
                        erros.append(f'Erro ao verificar assinatura RSA do .db: {e}')
                else:
                    erros.append('Assinatura RSA do .db ausente no .sig')
            else:
                erros.append('Arquivo .db não encontrado no ZIP')

            # PDF (opcional)
            pdf_names = [n for n in names if n.endswith('.pdf')]
            if pdf_names and payload.get('hash_pdf'):
                h = _sha256_entry(pdf_names[0])
                if h != payload['hash_pdf']:
                    erros.append(f'Hash PDF diverge: esperado={payload["hash_pdf"]} calculado={h}')

            # Fotos
            for arcname, expected_hash in (payload.get('hash_fotos') or {}).items():
                if arcname in names:
                    h = _sha256_entry(arcname)
                    if h != expected_hash:
                        erros.append(f'Hash foto diverge [{arcname}]: esperado={expected_hash} calculado={h}')
                else:
                    erros.append(f'Foto declarada no .sig não encontrada no ZIP: {arcname}')

    except zipfile.BadZipFile:
        return {'ok': False, 'erros': ['Arquivo ZIP corrompido ou inválido'], 'payload': {}}
    except Exception as e:
        return {'ok': False, 'erros': [f'Erro ao validar .sig: {e}'], 'payload': payload}

    return {'ok': len(erros) == 0, 'erros': erros, 'payload': payload}


def _buscar_token_cliente(db, cnpj: str) -> str:
    """Retorna o token da licença do cliente pelo CNPJ (normalizado, só dígitos).

    A coluna `clientes.cnpj` pode conter múltiplos CNPJs separados por vírgula
    (mesmo padrão usado em `idcelular` nas rotas de carga). Por isso o valor é
    dividido por vírgula e cada parte é normalizada e comparada individualmente
    — normalizar a coluna inteira de uma vez removeria a vírgula e fundiria os
    CNPJs, impedindo qualquer match exato.
    """
    cnpj_digits = re.sub(r'\D', '', cnpj or '')
    row = db.execute(
        text("""
            SELECT token FROM clientes
            WHERE :cnpj IN (
                SELECT regexp_replace(x, '[^0-9]', '', 'g')
                FROM unnest(string_to_array(cnpj, ',')) AS x
            )
            LIMIT 1
        """),
        {"cnpj": cnpj_digits},
    ).fetchone()
    if not row:
        print(f"[upload-contagem] cliente NAO encontrado cnpj={cnpj!r} digits={cnpj_digits}")
        return ''
    tok = (row[0] or '').strip()
    print(f"[upload-contagem] cliente OK cnpj={cnpj_digits} token_len={len(tok)} pref={tok[:6]}")
    return tok


# ==============================
# AUTH
# ==============================

API_TOKEN = os.getenv("API_TOKEN", "")

# Chave que assina os tokens de licença. Necessária para /validar-sig conferir
# se um `serial` vindo de um .sig é um token legítimo, mesmo que já tenha sido
# substituído por uma regeração posterior (troca de plano, renovação).
MASTER_KEY = (os.getenv("MASTER_KEY", "") or "").strip().strip("'\"")


def _b64url_decode(s: str) -> bytes:
    s = (s or "").replace('-', '+').replace('_', '/')
    pad = 4 - len(s) % 4
    if pad < 4:
        s += '=' * pad
    return base64.b64decode(s)


def verificar_assinatura_token(token_str: str) -> dict:
    """Valida a assinatura HMAC-SHA256 de um token de licença e devolve o payload.

    Formato: ``Base64Url(JSON_payload).Base64Url(HMAC_SHA256)``, assinado com a
    MASTER_KEY — mesma verificação que o coletor faz em `_verify_db_token`.

    Só a MASTER_KEY permite forjar um token, então um token cuja assinatura
    confere é autêntico mesmo que não seja mais o token corrente do cliente.

    Levanta ValueError se o formato ou a assinatura forem inválidos.
    """
    if not MASTER_KEY:
        raise ValueError('MASTER_KEY nao configurada no servidor')

    parts = (token_str or '').strip().split('.')
    if len(parts) != 2:
        raise ValueError('Token com formato invalido (esperado payload.assinatura)')

    payload_bytes = _b64url_decode(parts[0])
    sig_recebida = _b64url_decode(parts[1])

    esperada = hmac.new(MASTER_KEY.encode('utf-8'), payload_bytes, hashlib.sha256).digest()
    if not hmac.compare_digest(esperada, sig_recebida):
        raise ValueError('Assinatura do token invalida')

    return json.loads(payload_bytes.decode('utf-8'))

def _normalizar_token(raw: str) -> str:
    """Remove prefixo 'Bearer ' (case-insensitive) para normalizar comparação.

    O CSCollect envia 'Bearer <token>' enquanto o CSCollectManager envia
    apenas '<token>'. A comparação é feita sempre sobre o valor puro.
    """
    s = (raw or "").strip()
    if s.lower().startswith("bearer "):
        s = s[7:].strip()
    return s

def verificar_token(authorization: str):
    token_recebido = _normalizar_token(authorization)
    token_esperado = _normalizar_token(API_TOKEN)
    if not token_esperado or token_recebido != token_esperado:
        raise HTTPException(status_code=401, detail="Token inválido")

# ==============================
# ROTAS
# ==============================

@app.get("/")
def inicio():
    return {"status": "API funcionando"}

@app.get("/health")
def health():
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return {"status": "ok"}

# ------------------------------
# Upload de carga (enviado pelo manager)
# Identifica o destino pelo cnpj + idcelular + codvendedor
# ------------------------------
@app.post("/upload")
async def upload(
    file: UploadFile = File(...),
    cnpj: str = Form(...),
    idcelular: str = Form(...),
    codvendedor: str = Form(...),
    authorization: str = Header(...)
):
    verificar_token(authorization)

    conteudo = await file.read()

    url_arquivo = f"/download/{cnpj}/{codvendedor}/{file.filename}"

    db = Session()
    try:
        # Busca cliente_id pela tabela clientes
        row = db.execute(
            text("SELECT id FROM clientes WHERE cnpj = :cnpj"),
            {"cnpj": cnpj}
        ).fetchone()
        cliente_id = row[0] if row else None

        data_envio = _agora_local()

        _salvar_arquivo(db, 'carga', cnpj, codvendedor, file.filename, conteudo)
        db.execute(
            text("""
                INSERT INTO cargas (cnpj, nome_arquivo, url_arquivo, idcelular, codvendedor, cliente_id, data_envio)
                VALUES (:cnpj, :nome, :url, :idcelular, :codvendedor, :cliente_id, :data_envio)
            """),
            {
                "cnpj": cnpj,
                "nome": file.filename,
                "url": url_arquivo,
                "idcelular": idcelular,
                "codvendedor": codvendedor,
                "cliente_id": cliente_id,
                "data_envio": data_envio
            }
        )
        db.commit()
    finally:
        db.close()

    return {
        "ok": True,
        "cnpj": cnpj,
        "idcelular": idcelular,
        "codvendedor": codvendedor,
        "arquivo": file.filename,
        "url_arquivo": url_arquivo
    }

# ------------------------------
# Última carga de um celular/cnpj
# ------------------------------
@app.get("/ultima")
def ultima(
    cnpj: str,
    idcelular: str,
    codvendedor: str,
    authorization: str = Header(...)
):
    verificar_token(authorization)

    # idcelular pode vir com vírgulas: "cel1,cel2,cel3"
    ids = [i.strip() for i in idcelular.split(",") if i.strip()]

    db = Session()
    try:
        placeholders = ", ".join(f":id{i}" for i in range(len(ids)))
        params: dict = {"cnpj": cnpj, "codvendedor": codvendedor, "horas": EXPIRACAO_HORAS}
        for i, v in enumerate(ids):
            params[f"id{i}"] = v

        # Só lista carga cujo arquivo ainda está disponível para download.
        carga = db.execute(
            text(f"""
                SELECT c.id, c.nome_arquivo, c.url_arquivo, c.data_envio, c.codvendedor
                FROM cargas c
                JOIN arquivos_transferencia a
                  ON a.tipo = 'carga' AND a.cnpj = c.cnpj
                 AND a.pasta = c.codvendedor AND a.nome_arquivo = c.nome_arquivo
                WHERE c.cnpj = :cnpj
                  AND c.codvendedor = :codvendedor
                  AND c.idcelular IN ({placeholders})
                  AND {_SQL_ARQUIVO_VALIDO}
                ORDER BY a.criado_em DESC
                LIMIT 1
            """),
            params
        ).fetchone()
    finally:
        db.close()

    if not carga:
        return {"erro": "Nenhuma carga encontrada"}

    return {
        "id": carga[0],
        "nome_arquivo": carga[1],
        "url_arquivo": carga[2],
        "data_envio": _fmt_data_envio(carga[3]),
        "codvendedor": carga[4]
    }

# ------------------------------
# Download de carga
# ------------------------------
@app.get("/download/{cnpj}/{codvendedor}/{nome}")
def download(cnpj: str, codvendedor: str, nome: str, authorization: str = Header(...)):
    verificar_token(authorization)

    db = Session()
    try:
        conteudo = _ler_arquivo(db, 'carga', cnpj, codvendedor, nome)

        if conteudo is None:
            # Expirada ou inexistente: apaga a referência na hora para que
            # /ultima não continue apontando para um arquivo indisponível.
            db.execute(
                text(
                    "DELETE FROM cargas "
                    "WHERE cnpj = :cnpj AND codvendedor = :codvendedor AND nome_arquivo = :nome"
                ),
                {"cnpj": cnpj, "codvendedor": codvendedor, "nome": nome}
            )
            db.commit()
            raise HTTPException(status_code=404, detail="Carga expirada ou não encontrada")
    finally:
        db.close()

    # O registro não é apagado aqui: se a transferência cair no meio, o app
    # consegue baixar de novo dentro da validade. A carga é consumida pelo
    # DELETE abaixo, que o app chama depois de salvar o arquivo.
    return _resposta_arquivo(conteudo, nome)

# ------------------------------
# Deletar carga após download confirmado
# ------------------------------
@app.delete("/download/{cnpj}/{codvendedor}/{nome}")
def deletar_carga(cnpj: str, codvendedor: str, nome: str, authorization: str = Header(...)):
    verificar_token(authorization)

    db = Session()
    try:
        _remover_arquivo(db, 'carga', cnpj, codvendedor, nome)
        db.execute(
            text(
                "DELETE FROM cargas "
                "WHERE cnpj = :cnpj AND codvendedor = :codvendedor AND nome_arquivo = :nome"
            ),
            {"cnpj": cnpj, "codvendedor": codvendedor, "nome": nome}
        )
        db.commit()
    finally:
        db.close()

    return JSONResponse(status_code=200, content={"ok": True, "mensagem": f"Carga '{nome}' removida com sucesso."})

# ------------------------------
# Deletar carga por id (alternativa para o APK)
# ------------------------------
@app.delete("/carga/{carga_id}")
def deletar_carga_por_id(carga_id: int, authorization: str = Header(...)):
    verificar_token(authorization)

    db = Session()
    try:
        row = db.execute(
            text("SELECT cnpj, codvendedor, nome_arquivo FROM cargas WHERE id = :id"),
            {"id": carga_id}
        ).fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Carga não encontrada.")

        cnpj, codvendedor, nome = row[0], row[1], row[2]
        _remover_arquivo(db, 'carga', cnpj, codvendedor, nome)

        db.execute(text("DELETE FROM cargas WHERE id = :id"), {"id": carga_id})
        db.commit()
    finally:
        db.close()

    return JSONResponse(status_code=200, content={"ok": True, "mensagem": f"Carga {carga_id} removida com sucesso."})

# ------------------------------
# Upload de contagem (enviado pelo celular)
# ------------------------------
@app.post("/upload-contagem")
async def upload_contagem(
    file: UploadFile = File(...),
    cnpj: str = Form(...),
    idcelular: str = Form(...),
    authorization: str = Header(...)
):
    verificar_token(authorization)

    conteudo = await file.read()

    # Validar assinatura .sig antes de aceitar o arquivo
    db_val = Session()
    try:
        token = _buscar_token_cliente(db_val, cnpj)
    finally:
        db_val.close()

    resultado_sig = validar_sig_bytes(conteudo, token)
    if not resultado_sig['ok']:
        raise HTTPException(
            status_code=422,
            detail={
                "erro": "Validação de assinatura falhou",
                "detalhes": resultado_sig['erros']
            }
        )

    url_arquivo = f"/download-contagem/{cnpj}/{idcelular}/{file.filename}"

    db = Session()
    try:
        data_envio = _agora_local()

        _salvar_arquivo(db, 'contagem', cnpj, idcelular, file.filename, conteudo)
        db.execute(
            text("""
                INSERT INTO contagens (cnpj, idcelular, nome_arquivo, url_arquivo, data_envio)
                VALUES (:cnpj, :idcelular, :nome, :url, :data_envio)
            """),
            {
                "cnpj": cnpj,
                "idcelular": idcelular,
                "nome": file.filename,
                "url": url_arquivo,
                "data_envio": data_envio
            }
        )
        db.commit()
    finally:
        db.close()

    return {
        "ok": True,
        "cnpj": cnpj,
        "idcelular": idcelular,
        "arquivo": file.filename,
        "url_arquivo": url_arquivo
    }

# ------------------------------
# Última contagem de um celular/cnpj
# ------------------------------
@app.get("/ultima-contagem")
def ultima_contagem(
    cnpj: str,
    idcelular: str,
    authorization: str = Header(...)
):
    verificar_token(authorization)

    db = Session()
    try:
        contagem = db.execute(
            text(f"""
                SELECT c.nome_arquivo, c.url_arquivo, c.data_envio
                FROM contagens c
                JOIN arquivos_transferencia a
                  ON a.tipo = 'contagem' AND a.cnpj = c.cnpj
                 AND a.pasta = c.idcelular AND a.nome_arquivo = c.nome_arquivo
                WHERE c.cnpj = :cnpj AND c.idcelular = :idcelular
                  AND {_SQL_ARQUIVO_VALIDO}
                ORDER BY a.criado_em DESC
                LIMIT 1
            """),
            {"cnpj": cnpj, "idcelular": idcelular, "horas": EXPIRACAO_HORAS}
        ).fetchone()
    finally:
        db.close()

    if not contagem:
        return {"erro": "Nenhuma contagem encontrada"}

    return {
        "nome_arquivo": contagem[0],
        "url_arquivo": contagem[1],
        "data_envio": _fmt_data_envio(contagem[2])
    }

# ------------------------------
# Download de contagem
# ------------------------------
@app.get("/download-contagem/{cnpj}/{idcelular}/{nome}")
def download_contagem(cnpj: str, idcelular: str, nome: str, authorization: str = Header(...)):
    verificar_token(authorization)

    # O registro em `contagens` não é apagado aqui: o Manager remove o seu
    # após confirmar o 404, e a limpeza periódica cuida dos que sobrarem.
    db_chk = Session()
    try:
        conteudo = _ler_arquivo(db_chk, 'contagem', cnpj, idcelular, nome)
    finally:
        db_chk.close()

    if conteudo is None:
        raise HTTPException(status_code=404, detail="Contagem expirada ou não encontrada")

    # Re-validar assinatura .sig antes de servir o arquivo
    db_val = Session()
    try:
        token = _buscar_token_cliente(db_val, cnpj)
    finally:
        db_val.close()

    resultado_sig = validar_sig_bytes(conteudo, token)
    if not resultado_sig['ok']:
        raise HTTPException(
            status_code=422,
            detail={
                "erro": "Arquivo com assinatura inválida — download bloqueado",
                "detalhes": resultado_sig['erros']
            }
        )

    return _resposta_arquivo(conteudo, nome)

# ------------------------------
# Deletar contagem após download confirmado
# ------------------------------
@app.delete("/download-contagem/{cnpj}/{idcelular}/{nome}")
def deletar_contagem(cnpj: str, idcelular: str, nome: str, authorization: str = Header(...)):
    verificar_token(authorization)

    db = Session()
    try:
        removido = _remover_arquivo(db, 'contagem', cnpj, idcelular, nome)
        db.commit()
    finally:
        db.close()

    if not removido:
        raise HTTPException(status_code=404, detail=f"Arquivo '{nome}' não encontrado.")

    return JSONResponse(status_code=200, content={"ok": True, "mensagem": f"Arquivo '{nome}' removido com sucesso."})

# ------------------------------
# Listar contagens por CNPJ (usado pelo CSCollectManager como fallback HTTP)
# ------------------------------
@app.get("/contagens")
def listar_contagens(cnpj: str, authorization: str = Header(...)):
    verificar_token(authorization)

    db = Session()
    try:
        rows = db.execute(
            text(f"""
                SELECT c.id, c.cnpj, c.idcelular, c.nome_arquivo, c.url_arquivo, c.data_envio
                  FROM contagens c
                  JOIN arquivos_transferencia a
                    ON a.tipo = 'contagem' AND a.cnpj = c.cnpj
                   AND a.pasta = c.idcelular AND a.nome_arquivo = c.nome_arquivo
                 WHERE c.cnpj = :cnpj
                   AND {_SQL_ARQUIVO_VALIDO}
                 ORDER BY a.criado_em DESC
            """),
            {"cnpj": cnpj, "horas": EXPIRACAO_HORAS}
        ).fetchall()
    finally:
        db.close()

    return [
        {
            "id": r[0],
            "cnpj": r[1],
            "idcelular": r[2],
            "nome_arquivo": r[3],
            "url_arquivo": r[4],
            "data_envio": _fmt_data_envio(r[5]),
        }
        for r in rows
    ]


# ------------------------------
# Deletar contagem por ID (usado pelo CSCollectManager como fallback HTTP)
# ------------------------------
@app.delete("/contagem/{contagem_id}")
def deletar_contagem_por_id(contagem_id: int, authorization: str = Header(...)):
    verificar_token(authorization)

    db = Session()
    try:
        row = db.execute(
            text("SELECT cnpj, idcelular, nome_arquivo FROM contagens WHERE id = :id"),
            {"id": contagem_id}
        ).fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Contagem não encontrada.")

        cnpj, idcelular, nome = row[0], row[1], row[2]
        _remover_arquivo(db, 'contagem', cnpj, idcelular, nome)

        db.execute(text("DELETE FROM contagens WHERE id = :id"), {"id": contagem_id})
        db.commit()
    finally:
        db.close()

    return JSONResponse(status_code=200, content={"ok": True, "mensagem": f"Contagem {contagem_id} removida com sucesso."})


# ------------------------------
# TESTE BANCO
# ------------------------------
@app.get("/teste-db")
def teste_db(authorization: str = Header(...)):
    verificar_token(authorization)

    db = Session()
    try:
        result = db.execute(
            text("SELECT id, cnpj, idcelular, nome_arquivo, data_envio FROM cargas ORDER BY data_envio DESC LIMIT 20")
        ).fetchall()
    finally:
        db.close()

    return {"cargas": [dict(r._mapping) for r in result]}

# ------------------------------
# Validacao de licenca mobile
# Recebe cnpj + device_id, consulta tabela clientes,
# retorna dados para o cliente validar localmente.
# ------------------------------
from pydantic import BaseModel

class ValidarLicencaRequest(BaseModel):
    cnpj: str
    device_id: str


def _buscar_registro_licenca(db, cnpj: str):
    params = {
        'cnpj': cnpj,
        'like1': f'%,{cnpj}',
        'like2': f'{cnpj},%',
        'like3': f'%,{cnpj},%',
    }

    # Esquema atual (v6+): inclui arq_licenca e campos de API criptografados.
    try:
        row = db.execute(
            text("""
                SELECT cnpj, idcelular, token, arq_licenca, validade, ativo, nome_cliente,
                       sql_servidor, sql_banco, api_authorization, api_database_url
                FROM clientes
                WHERE cnpj = :cnpj
                   OR cnpj LIKE :like1
                   OR cnpj LIKE :like2
                   OR cnpj LIKE :like3
                LIMIT 1
            """),
            params
        ).fetchone()
        if row is not None:
            return row
    except Exception as e:
        # Produção pode estar com schema legado (sem colunas novas). Tenta fallback.
        print(f"[licenca] query v6 falhou, tentando fallback legado: {e}")

    # Esquema legado: não possui arq_licenca/api_authorization/api_database_url.
    row_legacy = db.execute(
        text("""
            SELECT cnpj, idcelular, token, validade, ativo, nome_cliente,
                   sql_servidor, sql_banco
            FROM clientes
            WHERE cnpj = :cnpj
               OR cnpj LIKE :like1
               OR cnpj LIKE :like2
               OR cnpj LIKE :like3
            LIMIT 1
        """),
        params
    ).fetchone()

    if not row_legacy:
        return None

    # Normaliza para o formato esperado por _validar_e_montar_licenca (11 campos).
    return (
        row_legacy[0],  # cnpj
        row_legacy[1],  # idcelular
        row_legacy[2],  # token
        '',             # arq_licenca (indisponível no legado)
        row_legacy[3],  # validade
        row_legacy[4],  # ativo
        row_legacy[5],  # nome_cliente
        row_legacy[6],  # sql_servidor
        row_legacy[7],  # sql_banco
        '',             # api_authorization (indisponível no legado)
        '',             # api_database_url (indisponível no legado)
    )


def _buscar_registro_licenca_por_device(db, device_id: str):
    """Busca o cliente cujo `device_id` conste em `idcelular`, sem precisar
    saber o CNPJ de antemão.

    Usado por /ativar-online-device (primeira ativação do app mobile: nesse
    momento ainda não existe nenhuma licença local, então não há CNPJ para
    filtrar — só o Device ID do aparelho, que o admin já deve ter cadastrado
    manualmente em `clientes.idcelular` antes). Como não há um valor único
    para o WHERE (idcelular é uma lista separada por vírgula, e o Device ID
    pode estar em qualquer posição), busca todos os clientes ativos e casa
    o Device ID em Python — mesma lógica de correspondência que
    `_validar_e_montar_licenca` já faz internamente para `ids_no_banco`.
    """
    try:
        rows = db.execute(
            text("""
                SELECT cnpj, idcelular, token, arq_licenca, validade, ativo, nome_cliente,
                       sql_servidor, sql_banco, api_authorization, api_database_url
                FROM clientes
                WHERE ativo = true
            """)
        ).fetchall()
    except Exception as e:
        # Produção pode estar com schema legado (sem colunas novas). Mesmo
        # fallback de _buscar_registro_licenca.
        print(f"[licenca] query v6 (por device) falhou, tentando fallback legado: {e}")
        rows_legacy = db.execute(
            text("""
                SELECT cnpj, idcelular, token, validade, ativo, nome_cliente,
                       sql_servidor, sql_banco
                FROM clientes
                WHERE ativo = true
            """)
        ).fetchall()
        rows = [
            (r[0], r[1], r[2], '', r[3], r[4], r[5], r[6], r[7], '', '')
            for r in rows_legacy
        ]

    for row in rows:
        ids_no_banco = [x.strip() for x in str(row[1] or '').split(',') if x.strip()]
        if device_id in ids_no_banco:
            return row
    return None


def _validar_e_montar_licenca(row, cnpj: str, device_id: str):
    if not row:
        return {'ok': False, 'motivo': 'licenca_nao_encontrada_no_servidor', 'mensagem': 'CNPJ nao cadastrado'}

    (
        db_cnpj, db_idcelular_raw, db_token, db_arq_licenca, db_validade,
        db_ativo, db_nome, db_sql_servidor, db_sql_banco,
        db_api_auth, db_api_db_url
    ) = row

    if not db_ativo:
        return {'ok': False, 'motivo': 'licenca_desativada_no_servidor', 'mensagem': 'Licenca desativada'}
    if db_validade:
        try:
            from datetime import date as _date
            exp = datetime.strptime(str(db_validade)[:10], '%Y-%m-%d').date()
            if _date.today() > exp:
                return {'ok': False, 'motivo': 'licenca_expirada_no_servidor', 'mensagem': 'Licenca expirada'}
        except Exception:
            pass

    ids_no_banco = [x.strip() for x in str(db_idcelular_raw or '').split(',') if x.strip()]
    if device_id not in ids_no_banco:
        return {
            'ok': False,
            'motivo': 'licenca_nao_encontrada_no_servidor',
            'mensagem': f'device_id nao autorizado ({len(ids_no_banco)} IDs cadastrados)'
        }

    validade_str = str(db_validade)[:10] if db_validade else ''
    cnpjs = [x.strip() for x in str(db_cnpj or '').split(',') if x.strip()]
    ids = [x.strip() for x in str(db_idcelular_raw or '').split(',') if x.strip()]
    # IMPORTANTE: Para o APK (mobile), sempre retornar API_TOKEN como api_authorization.
    # O banco pode ter um valor criptografado (para CSCollectManager), mas o APK recebe
    # plaintext porque usa um secret compartilhado com a API.
    # O valor do banco (db_api_auth) é criptografado com MASTER_KEY — o APK não pode descriptografar.
    api_auth_out = _normalizar_token(API_TOKEN)
    
    # api_database_url: usar do banco (criptografado lá) ou fallback para DATABASE_URL
    api_db_out = str(db_api_db_url).strip() if db_api_db_url else ''
    if not api_db_out:
        api_db_out = str(DATABASE_URL or '').strip()

    api_url_out = os.getenv('API_PUBLIC_URL', 'https://cscollectapi.onrender.com').strip()
    return {
        'ok': True,
        'motivo': '',
        'mensagem': 'Licenca valida',
        'validade': validade_str,
        'nome_cliente': str(db_nome) if db_nome else '',
        'cnpjs': cnpjs,
        'ids': ids,
        'sql_servidor': str(db_sql_servidor) if db_sql_servidor else '',
        'sql_banco': str(db_sql_banco) if db_sql_banco else '',
        'token': str(db_token) if db_token else '',
        'arq_licenca': str(db_arq_licenca) if db_arq_licenca else '',
        'api_authorization': api_auth_out,
        'api_database_url': api_db_out,
        'api_url': api_url_out,
    }


def _nome_arquivo_licenca(nome_cliente: str, cnpj: str) -> str:
    base = nome_cliente or cnpj or 'cliente'
    safe = ''.join(ch for ch in str(base) if ch.isalnum() or ch in (' ', '_', '-')).strip().replace(' ', '_')
    return f"Licenca_CSCollectManager_{safe or 'cliente'}.key"


@app.get("/licenca/{cnpj}")
def licenca(cnpj: str, authorization: str = Header(...)):
    """Consulta rápida de status/validade/plano de um cliente, por CNPJ.

    Uso interno (CSCollectManager) para sincronizar o .key local com o
    Neon periodicamente — não exige device_id, diferente de /validar-licenca.
    """
    verificar_token(authorization)

    cnpj = (cnpj or '').strip()
    if not cnpj:
        raise HTTPException(status_code=400, detail='cnpj é obrigatório')

    db = Session()
    try:
        row = db.execute(
            text("""
                SELECT cnpj, ativo, validade, tipo_licenca, nome_cliente,
                       token, arq_licenca
                FROM clientes
                WHERE cnpj = :cnpj
                   OR cnpj LIKE :like1 OR cnpj LIKE :like2 OR cnpj LIKE :like3
                LIMIT 1
            """),
            {
                "cnpj": cnpj,
                "like1": f"%,{cnpj}", "like2": f"{cnpj},%", "like3": f"%,{cnpj},%",
            }
        ).fetchone()
    finally:
        db.close()

    if not row:
        raise HTTPException(status_code=404, detail="Cliente não encontrado")

    return {
        "cnpj": row.cnpj,
        "ativo": bool(row.ativo),
        "validade": row.validade.isoformat() if row.validade else None,
        "tipo_licenca": row.tipo_licenca,
        "nome_cliente": row.nome_cliente,
        # O token é a chave HMAC que assina o .sig das contagens. Sem ele o
        # CSCollectManager fica com o token antigo no .key após uma renovação
        # de licença e passa a rejeitar os arquivos do coletor como se
        # estivessem adulterados. Protegido pelo mesmo verificar_token() das
        # demais rotas.
        "token": (row.token or "").strip(),
        # Arquivo .key completo, como está no banco. Permite ao
        # CSCollectManager regravar a licença local inteira a cada validação —
        # mesma ideia da rotina do coletor, que rebaixa a licença e passa a
        # trabalhar com os dados atualizados.
        "arq_licenca": (row.arq_licenca or "").strip(),
    }


class ValidarSigRequest(BaseModel):
    assinatura: str
    payload: dict


@app.post("/validar-sig")
def validar_sig_endpoint(req: ValidarSigRequest, authorization: str = Header(...)):
    """Confirma se um .sig foi assinado por um token de licença legítimo.

    Existe porque o token do cliente é regerado a cada troca de plano ou
    renovação: um arquivo assinado antes da regeração deixa de conferir com o
    token corrente, embora seja perfeitamente autêntico. Como só quem tem a
    MASTER_KEY consegue emitir um token válido, verificar a assinatura do
    próprio `serial` prova a autenticidade do arquivo sem depender de qual
    token está vigente — e sem distribuir a MASTER_KEY para as máquinas.
    """
    verificar_token(authorization)

    payload = req.payload or {}
    assinatura = (req.assinatura or '').strip()
    serial = str(payload.get('serial') or '').strip()

    if not serial or not assinatura:
        return {'ok': False, 'motivo': 'sig_incompleto',
                'mensagem': 'payload.serial e assinatura sao obrigatorios'}

    # 1. O .sig é internamente consistente (não foi adulterado após a assinatura)?
    payload_json = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                              separators=(',', ':')).encode('utf-8')
    esperado = hmac.new(serial.encode('utf-8'), payload_json, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(esperado, assinatura):
        return {'ok': False, 'motivo': 'hmac_invalido',
                'mensagem': 'Assinatura nao confere com o payload — arquivo adulterado'}

    # 2. O serial é um token realmente emitido pela CEOsoftware?
    try:
        token_payload = verificar_assinatura_token(serial)
    except ValueError as e:
        return {'ok': False, 'motivo': 'token_nao_autentico', 'mensagem': str(e)}
    except Exception as e:
        return {'ok': False, 'motivo': 'erro_validacao_token', 'mensagem': str(e)}

    # 3. O CNPJ do arquivo está entre os autorizados nesse token?
    cnpj_arquivo = re.sub(r'\D', '', str(payload.get('cnpj') or ''))
    cnpjs_token = [re.sub(r'\D', '', str(c)) for c in (token_payload.get('cnpjs') or [])]
    if cnpj_arquivo and cnpjs_token and cnpj_arquivo not in cnpjs_token:
        return {'ok': False, 'motivo': 'cnpj_nao_autorizado_no_token',
                'mensagem': f'CNPJ {cnpj_arquivo} nao autorizado no token que assinou o arquivo'}

    return {
        'ok': True,
        'motivo': '',
        'mensagem': 'Assinatura autentica',
        'validade_token': token_payload.get('validade', ''),
        'tipo_licenca_token': token_payload.get('tipo_licenca', ''),
        'gerado_em_token': token_payload.get('gerado_em', ''),
    }


@app.post("/validar-licenca")
@limiter.limit("10/minute")
def validar_licenca(req: ValidarLicencaRequest, request: Request):
    cnpj = (req.cnpj or '').strip()
    device_id = (req.device_id or '').strip()

    if not cnpj or not device_id:
        return {'ok': False, 'motivo': 'parametros_invalidos', 'mensagem': 'cnpj e device_id sao obrigatorios'}

    db = Session()
    try:
        row = _buscar_registro_licenca(db, cnpj)
        payload = _validar_e_montar_licenca(row, cnpj, device_id)
        if payload.get('ok'):
            payload['download_url'] = f'/download-licenca?cnpj={cnpj}&device_id={device_id}'
        return payload
    except Exception as e:
        # Evita HTTP 500 sem corpo JSON (quebra o cliente mobile no parse).
        print(f"[licenca] validar_licenca erro interno: {e}")
        return {
            'ok': False,
            'motivo': 'erro_servidor_licenca',
            'mensagem': 'Falha interna ao validar licença. Tente novamente em instantes.'
        }
    finally:
        db.close()


@app.get("/download-licenca")
def download_licenca(cnpj: str, device_id: str):
    cnpj = (cnpj or '').strip()
    device_id = (device_id or '').strip()

    if not cnpj or not device_id:
        raise HTTPException(status_code=400, detail='cnpj e device_id sao obrigatorios')

    db = Session()
    try:
        row = _buscar_registro_licenca(db, cnpj)
    finally:
        db.close()

    payload = _validar_e_montar_licenca(row, cnpj, device_id)
    if not payload.get('ok'):
        raise HTTPException(status_code=404, detail=payload.get('mensagem', 'Licenca nao encontrada'))

    conteudo = payload.get('arq_licenca') or payload.get('token')
    if not conteudo:
        raise HTTPException(status_code=404, detail='Licenca encontrada, mas sem arquivo disponivel')

    nome_arquivo = _nome_arquivo_licenca(payload.get('nome_cliente', ''), cnpj)
    return Response(
        content=str(conteudo).encode('utf-8'),
        media_type='application/octet-stream',
        headers={'Content-Disposition': f'attachment; filename="{nome_arquivo}"'}
    )


# ------------------------------
# Ativação Online — sem arquivo .key
# O operador gera um token avulso no CSCollectLicence e envia ao usuário.
# O app troca esse token (uso único, TTL curto) pela licença completa.
# ------------------------------

class AtivarOnlineRequest(BaseModel):
    cnpj: str
    device_id: str
    activation_token: str   # raw token recebido do operador (43 chars URL-safe)


@app.post("/ativar-online")
@limiter.limit("5/minute")
def ativar_online(req: AtivarOnlineRequest, request: Request):
    cnpj       = (req.cnpj or '').strip()
    device_id  = (req.device_id or '').strip()
    raw_token  = (req.activation_token or '').strip()

    if not cnpj or not device_id or not raw_token:
        return {'ok': False, 'motivo': 'parametros_invalidos', 'mensagem': 'cnpj, device_id e activation_token sao obrigatorios'}

    import hashlib as _hl
    token_hash = _hl.sha256(raw_token.encode('utf-8')).hexdigest()

    db = Session()
    try:
        row_tok = db.execute(
            text("""
                SELECT id, cnpj, expira_em, usado_em, device_id_autorizado
                FROM activation_tokens
                WHERE token_hash = :hash
                LIMIT 1
            """),
            {'hash': token_hash}
        ).fetchone()

        if not row_tok:
            return {'ok': False, 'motivo': 'token_invalido', 'mensagem': 'Token de ativacao nao encontrado'}

        tok_id, tok_cnpj, tok_expira, tok_usado, tok_dev_autorizado = row_tok

        if tok_usado is not None:
            return {'ok': False, 'motivo': 'token_ja_utilizado', 'mensagem': 'Token ja foi utilizado'}

        from datetime import timezone as _tz
        now_utc = datetime.now(_tz.utc)
        if tok_expira and tok_expira < now_utc:
            return {'ok': False, 'motivo': 'token_expirado', 'mensagem': 'Token expirado'}

        # Verificar que o token pertence ao cnpj solicitado
        tok_cnpjs = [x.strip() for x in str(tok_cnpj or '').split(',') if x.strip()]
        if cnpj not in tok_cnpjs and tok_cnpj != cnpj:
            return {'ok': False, 'motivo': 'token_cnpj_mismatch', 'mensagem': 'Token nao pertence ao CNPJ informado'}

        # Verificar device_id: deve bater com um dos autorizados pelo operador
        ids_autorizados = [x.strip() for x in str(tok_dev_autorizado or '').split(',') if x.strip()]
        if ids_autorizados and device_id not in ids_autorizados:
            return {
                'ok': False,
                'motivo': 'device_id_nao_autorizado',
                'mensagem': 'Este token foi gerado para outro dispositivo'
            }

        # Registrar device_id em clientes.idcelular (se ainda não estiver)
        row_lic = _buscar_registro_licenca(db, cnpj)
        if row_lic:
            ids_atuais = [x.strip() for x in str(row_lic[1] or '').split(',') if x.strip()]
            if device_id not in ids_atuais:
                ids_atuais.append(device_id)
                novo_ids = ','.join(ids_atuais)
                db.execute(
                    text("UPDATE clientes SET idcelular = :ids WHERE cnpj = :cnpj"),
                    {'ids': novo_ids, 'cnpj': row_lic[0]}
                )
                print(f'[ativar-online] device_id {device_id} registrado em clientes cnpj={cnpj}')

        # Buscar registro atualizado e montar payload
        row_lic = _buscar_registro_licenca(db, cnpj)
        payload = _validar_e_montar_licenca(row_lic, cnpj, device_id)

        if payload.get('ok'):
            # Marcar token como usado
            db.execute(
                text("""
                    UPDATE activation_tokens
                    SET usado_em = :now, device_id_usado = :dev
                    WHERE id = :id
                """),
                {'now': now_utc, 'dev': device_id, 'id': tok_id}
            )
            db.commit()
            payload['download_url'] = f'/download-licenca?cnpj={cnpj}&device_id={device_id}'

        return payload

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f'Erro interno: {e}')
    finally:
        db.close()


class AtivarOnlineDeviceRequest(BaseModel):
    device_id: str


@app.post("/ativar-online-device")
@limiter.limit("10/minute")
def ativar_online_device(req: AtivarOnlineDeviceRequest, request: Request):
    """Ativa o dispositivo só com o Device ID — sem CNPJ e sem token avulso
    (diferente de /validar-licenca e /ativar-online, que exigem um dos dois).

    Usado pelo botão "Ativar online" da tela de ativação inicial do app
    mobile (CSCollect), quando ainda não existe nenhuma licença/.key local
    para saber o CNPJ. Pré-requisito: o admin já cadastrou este Device ID em
    `clientes.idcelular` (mesma exigência de qualquer forma de ativação —
    aqui só muda COMO o cliente é encontrado, não se precisa de cadastro
    prévio). Mesmo formato de resposta de /validar-licenca — reaproveita
    _validar_e_montar_licenca integralmente.
    """
    device_id = (req.device_id or '').strip()
    if not device_id:
        return {'ok': False, 'motivo': 'parametros_invalidos', 'mensagem': 'device_id e obrigatorio'}

    db = Session()
    try:
        row = _buscar_registro_licenca_por_device(db, device_id)
        if not row:
            return {
                'ok': False,
                'motivo': 'licenca_nao_encontrada_no_servidor',
                'mensagem': 'Nenhum cliente ativo com este Device ID cadastrado',
            }
        # `cnpj` não é usado dentro de _validar_e_montar_licenca (só o
        # `db_cnpj` vindo da própria linha) — não há um CNPJ conhecido de
        # antemão nesse fluxo, por isso passamos vazio.
        return _validar_e_montar_licenca(row, '', device_id)
    except Exception as e:
        # Mesmo cuidado de /validar-licenca: nunca devolver HTTP 500 sem
        # corpo JSON (quebraria o parse no app mobile).
        print(f"[licenca] ativar_online_device erro interno: {e}")
        return {
            'ok': False,
            'motivo': 'erro_servidor_licenca',
            'mensagem': 'Falha interna ao ativar. Tente novamente em instantes.',
        }
    finally:
        db.close()
