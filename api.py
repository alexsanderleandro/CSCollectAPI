from fastapi import FastAPI, UploadFile, File, Header, HTTPException
from fastapi.responses import FileResponse
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

def get_cliente(authorization: str):
    db = Session()

    cliente = db.execute(
        text("SELECT id, cnpj FROM clientes WHERE token = :t"),
        {"t": authorization}
    ).fetchone()

    if not cliente:
        raise HTTPException(status_code=401, detail="Token inválido")

    return cliente._mapping  # importante

# ==============================
# ROTAS
# ==============================

@app.get("/")
def inicio():
    return {"status": "API funcionando"}

# ------------------------------
# Upload de carga
# ------------------------------
@app.post("/upload")
async def upload(file: UploadFile = File(...), authorization: str = Header(...)):
    cliente = get_cliente(authorization)

    pasta = os.path.join(BASE_DIR, str(cliente["id"]))
    os.makedirs(pasta, exist_ok=True)

    caminho = os.path.join(pasta, file.filename)

    with open(caminho, "wb") as f:
        f.write(await file.read())

    db = Session()
    db.execute(
        text("INSERT INTO cargas (cliente_id, nome_arquivo) VALUES (:c, :n)"),
        {"c": cliente["id"], "n": file.filename}
    )
    db.commit()

    return {
        "ok": True,
        "cliente": cliente["id"],
        "arquivo": file.filename
    }

# ------------------------------
# Última carga
# ------------------------------
@app.get("/ultima")
def ultima(authorization: str = Header(...)):
    cliente = get_cliente(authorization)

    db = Session()
    carga = db.execute(
        text("""
            SELECT nome_arquivo
            FROM cargas
            WHERE cliente_id = :c
            ORDER BY data_envio DESC
            LIMIT 1
        """),
        {"c": cliente["id"]}
    ).fetchone()

    if not carga:
        return {"erro": "Nenhuma carga"}

    return {"arquivo": carga[0]}

# ------------------------------
# Download
# ------------------------------
@app.get("/download/{nome}")
def download(nome: str, authorization: str = Header(...)):
    cliente = get_cliente(authorization)

    caminho = os.path.join(BASE_DIR, str(cliente["id"]), nome)

    if not os.path.exists(caminho):
        raise HTTPException(status_code=404, detail="Arquivo não encontrado")

    return FileResponse(caminho)

# ------------------------------
# TESTE BANCO
# ------------------------------
@app.get("/teste-db")
def teste_db():
    db = Session()
    result = db.execute(text("SELECT * FROM clientes")).fetchall()
    return {"clientes": [dict(r._mapping) for r in result]}