import os
import requests

PASTA_CARGAS = r"C:\Users\alex.CEOSOFTWAREAD\Documents\Python\VSCode\CSCollectAPI\cargas"
URL = "http://localhost:8000/upload"

ARQUIVO_CONTROLE = "ultimo_enviado.txt"


def pegar_ultimo_arquivo(pasta):
    arquivos = [
        os.path.join(pasta, f)
        for f in os.listdir(pasta)
        if f.endswith(".zip")
    ]

    if not arquivos:
        return None

    return max(arquivos, key=os.path.getmtime)


def ler_ultimo_enviado():
    if not os.path.exists(ARQUIVO_CONTROLE):
        return None

    with open(ARQUIVO_CONTROLE, "r") as f:
        return f.read().strip()


def salvar_ultimo_enviado(nome):
    with open(ARQUIVO_CONTROLE, "w") as f:
        f.write(nome)


def enviar_arquivo(caminho):
    nome = os.path.basename(caminho)

    headers = {
        "Authorization": "abc123"
    }

    with open(caminho, "rb") as f:
        files = {"file": (nome, f)}
        resp = requests.post(URL, files=files, headers=headers)

    print(resp.json())
    salvar_ultimo_enviado(nome)


if __name__ == "__main__":
    arquivo = pegar_ultimo_arquivo(PASTA_CARGAS)

    if not arquivo:
        print("Nenhuma carga encontrada.")
        exit()

    nome = os.path.basename(arquivo)
    ultimo = ler_ultimo_enviado()

    if nome == ultimo:
        print("Arquivo já enviado:", nome)
    else:
        print("Enviando:", nome)
        enviar_arquivo(arquivo)