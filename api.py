from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
import os

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

app = FastAPI()

# garante pasta local
BASE_DIR = "cargas"
os.makedirs(BASE_DIR, exist_ok=True)

# ==============================
# AUTH
# ==============================

API_TOKEN = os.getenv("API_TOKEN", "")

def verificar_token(authorization: str):
    if not API_TOKEN or authorization != API_TOKEN:
        raise HTTPException(status_code=401, detail="Token inválido")

# ==============================
# ROTAS
# ==============================

@app.get("/")
def inicio():
    return {"status": "API funcionando"}

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

        db.execute(
            text("""
                INSERT INTO cargas (cnpj, nome_arquivo, url_arquivo, idcelular, codvendedor, cliente_id)
                VALUES (:cnpj, :nome, :url, :idcelular, :codvendedor, :cliente_id)
            """),
            {
                "cnpj": cnpj,
                "nome": file.filename,
                "url": url_arquivo,
                "idcelular": idcelular,
                "codvendedor": codvendedor,
                "cliente_id": cliente_id
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

    pasta = os.path.join("contagens", cnpj, idcelular)
    os.makedirs(pasta, exist_ok=True)

    caminho = os.path.join(pasta, file.filename)

    conteudo = await file.read()
    with open(caminho, "wb") as f:
        f.write(conteudo)

    url_arquivo = f"/download-contagem/{cnpj}/{idcelular}/{file.filename}"

    db = Session()
    try:
        db.execute(
            text("""
                INSERT INTO contagens (cnpj, idcelular, nome_arquivo, url_arquivo)
                VALUES (:cnpj, :idcelular, :nome, :url)
            """),
            {
                "cnpj": cnpj,
                "idcelular": idcelular,
                "nome": file.filename,
                "url": url_arquivo
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