from contextlib import asynccontextmanager
from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import asyncio
import hashlib
import hmac
import io
import json
import os
import zipfile
from datetime import datetime, timedelta, timezone

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
INTERVALO_LIMPEZA_SEGUNDOS = 5 * 60  # verifica a cada 5 minutos


def _limpar_expirados():
    """
    Remove do banco (Neon) e do disco (Render) todos os registros de cargas
    e contagens cujo data_envio seja anterior a (agora - EXPIRACAO_HORAS).
    Executado em background pela tarefa assíncrona.
    """
    limite = datetime.now(timezone.utc) - timedelta(hours=EXPIRACAO_HORAS)

    db = Session()
    try:
        # ---- Cargas expiradas ----
        cargas = db.execute(
            text("""
                SELECT cnpj, codvendedor, nome_arquivo
                FROM cargas
                WHERE data_envio < :limite
            """),
            {"limite": limite}
        ).fetchall()

        for row in cargas:
            cnpj, codvendedor, nome = row[0], row[1], row[2]
            caminho = os.path.join(BASE_DIR, cnpj, codvendedor, nome)
            if os.path.isfile(caminho):
                try:
                    os.remove(caminho)
                except Exception as e:
                    print(f"[LIMPEZA] Erro ao remover arquivo de carga '{caminho}': {e}")

        if cargas:
            db.execute(
                text("DELETE FROM cargas WHERE data_envio < :limite"),
                {"limite": limite}
            )
            print(f"[LIMPEZA] {len(cargas)} carga(s) expirada(s) removida(s).")

        # ---- Contagens expiradas ----
        contagens = db.execute(
            text("""
                SELECT cnpj, idcelular, nome_arquivo
                FROM contagens
                WHERE data_envio < :limite
            """),
            {"limite": limite}
        ).fetchall()

        for row in contagens:
            cnpj, idcelular, nome = row[0], row[1], row[2]
            caminho = os.path.join("contagens", cnpj, idcelular, nome)
            if os.path.isfile(caminho):
                try:
                    os.remove(caminho)
                except Exception as e:
                    print(f"[LIMPEZA] Erro ao remover arquivo de contagem '{caminho}': {e}")

        if contagens:
            db.execute(
                text("DELETE FROM contagens WHERE data_envio < :limite"),
                {"limite": limite}
            )
            print(f"[LIMPEZA] {len(contagens)} contagem(s) expirada(s) removida(s).")

        db.commit()
    except Exception as e:
        db.rollback()
        print(f"[LIMPEZA] Erro durante limpeza: {e}")
    finally:
        db.close()


async def _tarefa_limpeza():
    """Loop assíncrono que executa a limpeza periódica de registros expirados."""
    while True:
        await asyncio.sleep(INTERVALO_LIMPEZA_SEGUNDOS)
        try:
            _limpar_expirados()
        except Exception as e:
            print(f"[LIMPEZA] Exceção não tratada na tarefa de limpeza: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Inicia a tarefa de limpeza no startup e a cancela no shutdown."""
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

# garante pasta local
BASE_DIR = "cargas"
os.makedirs(BASE_DIR, exist_ok=True)

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
            # se serial vazio, omite validação HMAC e verifica apenas integridade

            # 4. Helper SHA-256 de entrada do ZIP
            def _sha256_entry(name):
                h = hashlib.sha256()
                with zf.open(name) as f:
                    for chunk in iter(lambda: f.read(65536), b''):
                        h.update(chunk)
                return h.hexdigest()

            # TXT
            txt_names = [n for n in names if n.endswith('.txt')]
            if txt_names:
                h = _sha256_entry(txt_names[0])
                if h != payload.get('hash_txt', ''):
                    erros.append(f'Hash TXT diverge: esperado={payload.get("hash_txt")} calculado={h}')
            else:
                erros.append('Arquivo TXT não encontrado no ZIP')

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
    """Retorna o token da licença do cliente pelo CNPJ, ou string vazia se não encontrado."""
    row = db.execute(
        text("SELECT token FROM clientes WHERE cnpj = :cnpj"),
        {"cnpj": cnpj}
    ).fetchone()
    return row[0] if row else ''


# ==============================
# AUTH
# ==============================

API_TOKEN = os.getenv("API_TOKEN", "")

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

    # pasta organizada por cnpj/codvendedor
    pasta = os.path.join(BASE_DIR, cnpj, codvendedor)
    os.makedirs(pasta, exist_ok=True)

    caminho = os.path.join(pasta, file.filename)

    conteudo = await file.read()
    with open(caminho, "wb") as f:
        f.write(conteudo)

    url_arquivo = f"/download/{cnpj}/{codvendedor}/{file.filename}"

    db = Session()
    try:
        # Busca cliente_id pela tabela clientes
        row = db.execute(
            text("SELECT id FROM clientes WHERE cnpj = :cnpj"),
            {"cnpj": cnpj}
        ).fetchone()
        cliente_id = row[0] if row else None

        data_envio = datetime.now(timezone.utc)

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
        params: dict = {"cnpj": cnpj, "codvendedor": codvendedor}
        for i, v in enumerate(ids):
            params[f"id{i}"] = v

        carga = db.execute(
            text(f"""
                SELECT id, nome_arquivo, url_arquivo, data_envio, codvendedor
                FROM cargas
                WHERE cnpj = :cnpj
                  AND codvendedor = :codvendedor
                  AND idcelular IN ({placeholders})
                ORDER BY data_envio DESC
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
        "data_envio": str(carga[3]),
        "codvendedor": carga[4]
    }

# ------------------------------
# Download — deleta registro do Neon antes de servir
# ------------------------------
@app.get("/download/{cnpj}/{codvendedor}/{nome}")
def download(cnpj: str, codvendedor: str, nome: str, authorization: str = Header(...)):
    verificar_token(authorization)

    caminho = os.path.join(BASE_DIR, cnpj, codvendedor, nome)

    if not os.path.exists(caminho):
        raise HTTPException(status_code=404, detail="Arquivo não encontrado")

    # Remove o registro do banco antes de servir o arquivo
    db = Session()
    try:
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

    # Serve o arquivo local
    # Alternativa: com open() para controle total do stream
    # with open(caminho, "rb") as f:
    #     conteudo = f.read()
    # return Response(content=conteudo, media_type="application/octet-stream")
    return FileResponse(caminho)

# ------------------------------
# Deletar carga após download confirmado
# ------------------------------
@app.delete("/download/{cnpj}/{codvendedor}/{nome}")
def deletar_carga(cnpj: str, codvendedor: str, nome: str, authorization: str = Header(...)):
    verificar_token(authorization)

    caminho = os.path.abspath(os.path.join(BASE_DIR, cnpj, codvendedor, nome))
    base_abs = os.path.abspath(BASE_DIR)

    if not caminho.startswith(base_abs + os.sep):
        raise HTTPException(status_code=400, detail="Nome de arquivo inválido.")

    # remove arquivo físico (ignora se já não existir)
    if os.path.isfile(caminho):
        os.remove(caminho)

    # remove registro do banco
    db = Session()
    try:
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
            text("SELECT cnpj, idcelular, nome_arquivo FROM cargas WHERE id = :id"),
            {"id": carga_id}
        ).fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Carga não encontrada.")

        cnpj, codvendedor, nome = row[0], row[1], row[2]
        caminho = os.path.join(BASE_DIR, cnpj, codvendedor, nome)

        if os.path.isfile(caminho):
            os.remove(caminho)

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

    pasta = os.path.join("contagens", cnpj, idcelular)
    os.makedirs(pasta, exist_ok=True)

    caminho = os.path.join(pasta, file.filename)

    with open(caminho, "wb") as f:
        f.write(conteudo)

    url_arquivo = f"/download-contagem/{cnpj}/{idcelular}/{file.filename}"

    db = Session()
    try:
        data_envio = datetime.now(timezone.utc)

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
            text("""
                SELECT nome_arquivo, url_arquivo, data_envio
                FROM contagens
                WHERE cnpj = :cnpj AND idcelular = :idcelular
                ORDER BY data_envio DESC
                LIMIT 1
            """),
            {"cnpj": cnpj, "idcelular": idcelular}
        ).fetchone()
    finally:
        db.close()

    if not contagem:
        return {"erro": "Nenhuma contagem encontrada"}

    return {
        "nome_arquivo": contagem[0],
        "url_arquivo": contagem[1],
        "data_envio": str(contagem[2])
    }

# ------------------------------
# Download de contagem
# ------------------------------
@app.get("/download-contagem/{cnpj}/{idcelular}/{nome}")
def download_contagem(cnpj: str, idcelular: str, nome: str, authorization: str = Header(...)):
    verificar_token(authorization)

    caminho = os.path.join("contagens", cnpj, idcelular, nome)

    if not os.path.exists(caminho):
        raise HTTPException(status_code=404, detail="Arquivo não encontrado")

    # Re-validar assinatura .sig antes de servir o arquivo
    with open(caminho, "rb") as f:
        conteudo = f.read()

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

    return FileResponse(caminho)

# ------------------------------
# Deletar contagem após download confirmado
# ------------------------------
@app.delete("/download-contagem/{cnpj}/{idcelular}/{nome}")
def deletar_contagem(cnpj: str, idcelular: str, nome: str, authorization: str = Header(...)):
    verificar_token(authorization)

    contagens_abs = os.path.abspath("contagens")
    caminho = os.path.abspath(os.path.join("contagens", cnpj, idcelular, nome))

    if not caminho.startswith(contagens_abs + os.sep):
        raise HTTPException(status_code=400, detail="Nome de arquivo inválido.")

    if not os.path.isfile(caminho):
        raise HTTPException(status_code=404, detail=f"Arquivo '{nome}' não encontrado.")

    try:
        os.remove(caminho)
        return JSONResponse(status_code=200, content={"ok": True, "mensagem": f"Arquivo '{nome}' removido com sucesso."})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao remover arquivo: {e}")

# ------------------------------
# Listar contagens por CNPJ (usado pelo CSCollectManager como fallback HTTP)
# ------------------------------
@app.get("/contagens")
def listar_contagens(cnpj: str, authorization: str = Header(...)):
    verificar_token(authorization)

    db = Session()
    try:
        rows = db.execute(
            text("""
                SELECT id, cnpj, idcelular, nome_arquivo, url_arquivo, data_envio
                  FROM contagens
                 WHERE cnpj = :cnpj
                 ORDER BY data_envio DESC
            """),
            {"cnpj": cnpj}
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
            "data_envio": str(r[5]) if r[5] else None,
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
        caminho = os.path.join("contagens", cnpj, idcelular, nome)

        if os.path.isfile(caminho):
            try:
                os.remove(caminho)
            except Exception:
                pass

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
    # Fallbacks para fluxo "ativação online sem .key":
    # - Se api_authorization não estiver preenchido no banco, usar API_TOKEN do ambiente.
    # - Se api_database_url não estiver preenchido, usar DATABASE_URL do ambiente.
    api_auth_out = str(db_api_auth).strip() if db_api_auth else ''
    if not api_auth_out:
        api_auth_out = _normalizar_token(API_TOKEN)

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

        # Verificar device_id: deve bater com o autorizado pelo operador
        if tok_dev_autorizado and tok_dev_autorizado.strip() != device_id:
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
