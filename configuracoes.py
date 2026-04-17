"""
configuracoes.py

Janela de configurações da aplicação CSCollect.

Permite ao usuário definir e alterar:
- URL da API
- Header de autorização (token)
- Caminho da pasta de cargas

As configurações são persistidas automaticamente no arquivo config.json.

Uso:
    python configuracoes.py
"""

import os
import sys
import json

from PySide6.QtWidgets import (
    QApplication,
    QWidget,
    QLabel,
    QLineEdit,
    QPushButton,
    QFileDialog,
    QVBoxLayout,
    QHBoxLayout,
    QMessageBox,
    QGroupBox,
    QFormLayout,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon

ARQUIVO_CONFIG = "config.json"

DEFAULTS = {
    "url": "https://cscollectapi.onrender.com",
    "autorizacao": "abc123",
    "pasta_cargas": "",
}


def carregar_config() -> dict:
    """
    Carrega as configurações salvas do arquivo config.json.

    Returns:
        dict: Dicionário com as configurações. Valores ausentes são
              preenchidos com os padrões definidos em DEFAULTS.
    """
    if os.path.exists(ARQUIVO_CONFIG):
        with open(ARQUIVO_CONFIG, "r", encoding="utf-8") as f:
            dados = json.load(f)
        return {**DEFAULTS, **dados}
    return dict(DEFAULTS)


def salvar_config(config: dict) -> None:
    """
    Persiste as configurações no arquivo config.json.

    Args:
        config (dict): Dicionário com as configurações a serem salvas.
    """
    with open(ARQUIVO_CONFIG, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4, ensure_ascii=False)


class JanelaConfiguracoes(QWidget):
    """
    Janela principal de configurações da aplicação.

    Exibe um formulário com os campos de URL, autorização e pasta de cargas,
    permitindo salvar ou cancelar as alterações.
    """

    def __init__(self) -> None:
        """Inicializa a janela e carrega as configurações existentes."""
        super().__init__()
        self.setWindowTitle("CSCollect – Configurações")
        self.setMinimumWidth(500)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowMaximizeButtonHint)

        self.config = carregar_config()
        self._build_ui()
        self._populate_fields()

    def _build_ui(self) -> None:
        """Constrói todos os widgets e layouts da janela."""
        layout_principal = QVBoxLayout(self)
        layout_principal.setSpacing(16)
        layout_principal.setContentsMargins(20, 20, 20, 20)

        # ── Grupo: API ───────────────────────────────────────────────────────
        grupo_api = QGroupBox("API")
        form_api = QFormLayout(grupo_api)
        form_api.setSpacing(10)

        self.campo_url = QLineEdit()
        self.campo_url.setPlaceholderText("https://exemplo.com")
        form_api.addRow("URL:", self.campo_url)

        self.campo_autorizacao = QLineEdit()
        self.campo_autorizacao.setPlaceholderText("Token de autorização")
        self.campo_autorizacao.setEchoMode(QLineEdit.Password)
        form_api.addRow("Autorização:", self.campo_autorizacao)

        layout_principal.addWidget(grupo_api)

        # ── Grupo: Pasta de Cargas ───────────────────────────────────────────
        grupo_pasta = QGroupBox("Pasta de Cargas")
        layout_pasta = QVBoxLayout(grupo_pasta)
        layout_pasta.setSpacing(8)

        layout_campo_pasta = QHBoxLayout()
        self.campo_pasta = QLineEdit()
        self.campo_pasta.setPlaceholderText("Selecione ou digite o caminho da pasta...")
        self.campo_pasta.setReadOnly(True)

        btn_selecionar = QPushButton("Selecionar…")
        btn_selecionar.setFixedWidth(110)
        btn_selecionar.clicked.connect(self._selecionar_pasta)

        layout_campo_pasta.addWidget(self.campo_pasta)
        layout_campo_pasta.addWidget(btn_selecionar)
        layout_pasta.addLayout(layout_campo_pasta)

        layout_principal.addWidget(grupo_pasta)

        # ── Botões ───────────────────────────────────────────────────────────
        layout_botoes = QHBoxLayout()
        layout_botoes.addStretch()

        btn_cancelar = QPushButton("Cancelar")
        btn_cancelar.setFixedWidth(100)
        btn_cancelar.clicked.connect(self.close)

        btn_salvar = QPushButton("Salvar")
        btn_salvar.setFixedWidth(100)
        btn_salvar.setDefault(True)
        btn_salvar.clicked.connect(self._salvar)

        layout_botoes.addWidget(btn_cancelar)
        layout_botoes.addWidget(btn_salvar)
        layout_principal.addLayout(layout_botoes)

    def _populate_fields(self) -> None:
        """Preenche os campos do formulário com os valores do config.json."""
        self.campo_url.setText(self.config.get("url", ""))
        self.campo_autorizacao.setText(self.config.get("autorizacao", ""))
        self.campo_pasta.setText(self.config.get("pasta_cargas", ""))

    def _selecionar_pasta(self) -> None:
        """
        Abre um diálogo para seleção de pasta e atualiza o campo correspondente.
        """
        pasta_atual = self.campo_pasta.text() or os.path.expanduser("~")
        pasta = QFileDialog.getExistingDirectory(
            self,
            "Selecionar pasta de cargas",
            pasta_atual,
            QFileDialog.ShowDirsOnly,
        )
        if pasta:
            self.campo_pasta.setText(pasta)

    def _salvar(self) -> None:
        """
        Valida os campos e persiste as configurações no config.json.

        Exibe mensagens de erro caso algum campo obrigatório esteja vazio
        ou a pasta informada não exista. Em caso de sucesso, fecha a janela.
        """
        url = self.campo_url.text().strip()
        autorizacao = self.campo_autorizacao.text().strip()
        pasta = self.campo_pasta.text().strip()

        if not url:
            self._erro("O campo URL é obrigatório.")
            self.campo_url.setFocus()
            return

        if not autorizacao:
            self._erro("O campo Autorização é obrigatório.")
            self.campo_autorizacao.setFocus()
            return

        if not pasta:
            self._erro("Selecione a pasta de cargas.")
            return

        if not os.path.isdir(pasta):
            self._erro(f"A pasta informada não existe:\n{pasta}")
            return

        self.config["url"] = url
        self.config["autorizacao"] = autorizacao
        self.config["pasta_cargas"] = pasta
        salvar_config(self.config)

        QMessageBox.information(self, "Sucesso", "Configurações salvas com sucesso.")
        self.close()

    def _erro(self, mensagem: str) -> None:
        """
        Exibe uma caixa de diálogo de erro.

        Args:
            mensagem (str): Texto a ser exibido na caixa de erro.
        """
        QMessageBox.critical(self, "Erro de validação", mensagem)


def main() -> None:
    """Ponto de entrada da aplicação de configurações."""
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    janela = JanelaConfiguracoes()
    janela.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
