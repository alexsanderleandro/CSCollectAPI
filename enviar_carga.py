"""
enviar_carga.py

Script responsável por monitorar uma pasta de arquivos .zip e enviar
o arquivo mais recente para a API CSCollect, evitando reenvios.

As configurações (URL, autorização e pasta) são lidas do config.json.
Use o formulário gráfico (configuracoes.py) para defini-las.

Uso:
    python enviar_carga.py           # Execução normal
    python enviar_carga.py --config  # Abrir janela de configurações
"""

import os
import sys
import json
import argparse
import requests

ARQUIVO_CONTROLE = "ultimo_enviado.txt"
ARQUIVO_CONFIG = "config.json"


def carregar_config():
    """
    Carrega as configurações salvas do arquivo config.json.

    Returns:
        dict: Dicionário com as configurações. Retorna um dicionário vazio
              caso o arquivo ainda não exista.
    """
    if os.path.exists(ARQUIVO_CONFIG):
        with open(ARQUIVO_CONFIG, "r") as f:
            return json.load(f)
    return {}


def salvar_config(config):
    """
    Persiste as configurações no arquivo config.json.

    Args:
        config (dict): Dicionário com as configurações a serem salvas.
    """
    with open(ARQUIVO_CONFIG, "w") as f:
        json.dump(config, f, indent=4)


def abrir_configuracoes() -> None:
    """
    Abre a janela gráfica de configurações (configuracoes.py) e aguarda o fechamento.
    """
    from PySide6.QtWidgets import QApplication
    from configuracoes import JanelaConfiguracoes

    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    janela = JanelaConfiguracoes()
    janela.show()
    app.exec()


def obter_config_validada() -> dict:
    """
    Retorna as configurações necessárias para o envio.

    Se algum campo obrigatório (url, autorizacao, pasta_cargas) estiver
    ausente ou inválido, abre automaticamente a janela de configurações
    e encerra o script após o fechamento, para que o usuário execute
    novamente.

    Returns:
        dict: Dicionário com url, autorizacao e pasta_cargas válidos.
    """
    config = carregar_config()
    url = config.get("url", "").strip()
    autorizacao = config.get("autorizacao", "").strip()
    pasta = config.get("pasta_cargas", "").strip()

    if not url or not autorizacao or not pasta or not os.path.isdir(pasta):
        print("Configurações incompletas ou inválidas. Abrindo janela de configurações...")
        abrir_configuracoes()
        print("Configurações salvas. Execute o script novamente.")
        sys.exit(0)

    return config


def pegar_ultimo_arquivo(pasta):
    """
    Retorna o arquivo .zip mais recente encontrado na pasta informada.

    Args:
        pasta (str): Caminho da pasta onde os arquivos serão buscados.

    Returns:
        str | None: Caminho completo do arquivo .zip mais recente,
                    ou None se nenhum arquivo for encontrado.
    """
    arquivos = [
        os.path.join(pasta, f)
        for f in os.listdir(pasta)
        if f.endswith(".zip")
    ]

    if not arquivos:
        return None

    return max(arquivos, key=os.path.getmtime)


def ler_ultimo_enviado():
    """
    Lê o nome do último arquivo enviado registrado no arquivo de controle.

    Returns:
        str | None: Nome do último arquivo enviado, ou None se o arquivo
                    de controle ainda não existir.
    """
    if not os.path.exists(ARQUIVO_CONTROLE):
        return None

    with open(ARQUIVO_CONTROLE, "r") as f:
        return f.read().strip()


def salvar_ultimo_enviado(nome):
    """
    Registra o nome do arquivo enviado no arquivo de controle.

    Args:
        nome (str): Nome do arquivo que foi enviado com sucesso.
    """
    with open(ARQUIVO_CONTROLE, "w") as f:
        f.write(nome)


def enviar_arquivo(caminho: str, url: str, autorizacao: str) -> None:
    """
    Envia um arquivo .zip para o endpoint de upload da API.

    Realiza uma requisição POST com o arquivo em multipart/form-data,
    utilizando o header de autorização. Após o envio bem-sucedido,
    registra o nome do arquivo no controle de envios.

    Args:
        caminho (str): Caminho completo do arquivo a ser enviado.
        url (str): URL base da API.
        autorizacao (str): Token de autorização para o header.
    """
    nome = os.path.basename(caminho)

    headers = {
        "Authorization": autorizacao
    }

    with open(caminho, "rb") as f:
        files = {"file": (nome, f)}
        resp = requests.post(f"{url}/upload", files=files, headers=headers)

    print(resp.json())
    salvar_ultimo_enviado(nome)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Enviar carga para a API")
    parser.add_argument("--config", action="store_true", help="Abrir janela de configurações")
    args = parser.parse_args()

    if args.config:
        abrir_configuracoes()
        sys.exit(0)

    config = obter_config_validada()
    pasta = config["pasta_cargas"]
    url = config["url"]
    autorizacao = config["autorizacao"]

    arquivo = pegar_ultimo_arquivo(pasta)

    if not arquivo:
        print("Nenhuma carga encontrada.")
        sys.exit(0)

    nome = os.path.basename(arquivo)
    ultimo = ler_ultimo_enviado()

    if nome == ultimo:
        print("Arquivo já enviado:", nome)
    else:
        print("Enviando:", nome)
        enviar_arquivo(arquivo, url, autorizacao)