# Validação do Arquivo `.sig` — CSCollect

Este documento descreve o formato do arquivo de assinatura `.sig` gerado pelo app CSCollect em cada exportação via API, e como o **manager/retaguarda** deve validá-lo antes de processar o ZIP recebido.

---

## 1. Estrutura do ZIP exportado

Cada ZIP enviado pelo app contém, no mínimo:

```
MOD1_1_043_65381113000120_070520261714.zip
├── MOD1_1_043_65381113000120_070520261714.txt   ← contagem em texto plano
├── MOD1_1_043_65381113000120_070520261714.pdf   ← relatório PDF (se gerado)
├── MOD1_1_043_65381113000120_070520261714.sig   ← assinatura (JSON)
└── fotos/                                        ← fotos de produtos (opcional)
    ├── produto_7891234560012.jpg
    └── ...
```

> O `.sig` é **sempre** gerado. O PDF e as fotos são opcionais.

---

## 2. Formato do arquivo `.sig`

O `.sig` é um arquivo JSON com dois campos raiz:

```json
{
  "assinatura": "<hex HMAC-SHA256 do payload>",
  "payload": {
    "algoritmo":       "HMAC-SHA256",
    "codempresa":      "1",
    "codvendedor":     "043",
    "cnpj":            "65381113000120",
    "hash_assinatura": "<sha256 da imagem de assinatura digital>",
    "hash_fotos": {
      "fotos/produto_7891234560012.jpg": "<sha256>"
    },
    "hash_pdf":        "<sha256 do PDF, ou vazio>",
    "hash_txt":        "<sha256 do TXT>",
    "idcelular":       "<device id do coletor>",
    "modelo":          "MOD1",
    "nome_arquivo":    "MOD1_1_043_65381113000120_070520261714.zip",
    "serial":          "<token da licença do cliente>",
    "timestamp":       "2026-05-07T17:14:00",
    "versao":          "26.05.07 rev. 3"
  }
}
```

### Campos do payload

| Campo | Tipo | Descrição |
|---|---|---|
| `algoritmo` | string | Sempre `"HMAC-SHA256"` |
| `codempresa` | string | Código da empresa configurado no app |
| `codvendedor` | string | Código do usuário/vendedor |
| `cnpj` | string | CNPJ sem pontuação (`65381113000120`) |
| `hash_assinatura` | string hex | SHA-256 dos bytes brutos da imagem de assinatura digital capturada no app |
| `hash_fotos` | object | Mapa `{ "fotos/<filename>": "<sha256>" }` de cada foto incluída no ZIP. Vazio `{}` se sem fotos |
| `hash_pdf` | string hex | SHA-256 do arquivo PDF. String vazia `""` se PDF não foi gerado |
| `hash_txt` | string hex | SHA-256 do arquivo TXT de contagem |
| `idcelular` | string | Identificador único do dispositivo coletor |
| `modelo` | string | `"MOD1"` ou `"MOD2"` |
| `nome_arquivo` | string | Nome exato do arquivo ZIP |
| `serial` | string | Token da licença (campo `token` do arquivo `.key`) |
| `timestamp` | string | ISO 8601 — `YYYY-MM-DDTHH:MM:SS` do momento da exportação |
| `versao` | string | Versão do app CSCollect que gerou o arquivo |

---

## 3. Como validar a assinatura

### 3.1 Algoritmo

A assinatura HMAC-SHA256 é calculada **sobre o JSON canônico do payload**:
- Chaves em **ordem alfabética** (`sort_keys=True`)
- **Sem espaços** após `,` e `:` (`separators=(',', ':')`)
- Encoding **UTF-8**

A **chave HMAC** é o valor do campo `serial` (token da licença do cliente), codificado em UTF-8.

### 3.2 Passo a passo

```
1. Extrair o arquivo .sig do ZIP recebido
2. Parsear o JSON e obter `payload` e `assinatura`
3. Obter o token do cliente no banco de dados (tabela clientes, campo token, WHERE cnpj = payload.cnpj)
4. Serializar payload em JSON canônico: sort_keys=True, separators=(',',':'), sem BOM
5. Calcular HMAC-SHA256(key=token.encode('utf-8'), msg=payload_json.encode('utf-8'))
6. Comparar (em tempo constante) o digest hex com o campo `assinatura`
7. Se iguais → assinatura válida. Prosseguir com as validações de integridade dos arquivos.
```

### 3.3 Validação de integridade dos arquivos

Após validar a assinatura HMAC, calcular SHA-256 de cada arquivo extraído do ZIP e comparar com os hashes declarados no payload:

```
hash_sha256(arquivo.txt) == payload.hash_txt
hash_sha256(arquivo.pdf) == payload.hash_pdf   (se hash_pdf != "")
hash_sha256(fotos/X.jpg) == payload.hash_fotos["fotos/X.jpg"]
```

Se qualquer hash divergir, o arquivo foi **adulterado** após a geração.

---

## 4. Implementação de referência (Python)

```python
import hashlib
import hmac
import json
import zipfile

def validar_sig(zip_path: str, token_cliente: str) -> dict:
    """
    Valida o arquivo .sig contido no ZIP exportado pelo CSCollect.

    Retorna dict com:
        ok          : bool   — True se assinatura e hashes são válidos
        erros       : list   — lista de mensagens de erro encontradas
        payload     : dict   — payload do .sig (mesmo se inválido)
    """
    erros = []
    payload = {}

    # 1. Abrir o ZIP e localizar o .sig
    with zipfile.ZipFile(zip_path, 'r') as zf:
        names = zf.namelist()
        sig_names = [n for n in names if n.endswith('.sig')]
        if not sig_names:
            return {'ok': False, 'erros': ['Arquivo .sig não encontrado no ZIP'], 'payload': {}}

        sig_content = zf.read(sig_names[0]).decode('utf-8')
        doc = json.loads(sig_content)
        payload     = doc.get('payload', {})
        assinatura  = doc.get('assinatura', '')

        # 2. Recalcular JSON canônico do payload
        payload_json  = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                   separators=(',', ':'))
        payload_bytes = payload_json.encode('utf-8')

        # 3. Validar HMAC
        expected_sig = hmac.new(
            token_cliente.encode('utf-8'),
            payload_bytes,
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(expected_sig, assinatura):
            erros.append('Assinatura HMAC inválida — token não confere ou payload adulterado')

        # 4. Validar hashes dos arquivos
        def _sha256_zip_entry(zf, name):
            h = hashlib.sha256()
            with zf.open(name) as f:
                for chunk in iter(lambda: f.read(65536), b''):
                    h.update(chunk)
            return h.hexdigest()

        # TXT
        txt_names = [n for n in names if n.endswith('.txt')]
        if txt_names:
            h = _sha256_zip_entry(zf, txt_names[0])
            if h != payload.get('hash_txt', ''):
                erros.append(f'Hash TXT diverge: esperado={payload.get("hash_txt")} calculado={h}')
        else:
            erros.append('Arquivo TXT não encontrado no ZIP')

        # PDF (opcional)
        pdf_names = [n for n in names if n.endswith('.pdf')]
        if pdf_names and payload.get('hash_pdf'):
            h = _sha256_zip_entry(zf, pdf_names[0])
            if h != payload['hash_pdf']:
                erros.append(f'Hash PDF diverge: esperado={payload["hash_pdf"]} calculado={h}')

        # Fotos
        for arcname, expected_hash in (payload.get('hash_fotos') or {}).items():
            if arcname in names:
                h = _sha256_zip_entry(zf, arcname)
                if h != expected_hash:
                    erros.append(f'Hash foto diverge [{arcname}]: esperado={expected_hash} calculado={h}')
            else:
                erros.append(f'Foto declarada no .sig não encontrada no ZIP: {arcname}')

    return {
        'ok':      len(erros) == 0,
        'erros':   erros,
        'payload': payload,
    }


# Exemplo de uso
if __name__ == '__main__':
    TOKEN = 'token_do_cliente_no_banco'  # buscar da tabela clientes WHERE cnpj = ...
    resultado = validar_sig('MOD1_1_043_65381113000120_070520261714.zip', TOKEN)
    if resultado['ok']:
        print('✅ Assinatura válida — arquivo íntegro')
    else:
        print('❌ Validação falhou:')
        for err in resultado['erros']:
            print(f'   • {err}')
```

---

## 5. Implementação de referência (Node.js / TypeScript)

```typescript
import crypto from 'crypto';
import AdmZip from 'adm-zip'; // npm install adm-zip

interface SigDoc {
  assinatura: string;
  payload: Record<string, unknown>;
}

interface ValidationResult {
  ok: boolean;
  erros: string[];
  payload: Record<string, unknown>;
}

function sha256Buffer(buf: Buffer): string {
  return crypto.createHash('sha256').update(buf).digest('hex');
}

export function validarSig(zipPath: string, tokenCliente: string): ValidationResult {
  const erros: string[] = [];
  const zip = new AdmZip(zipPath);
  const entries = zip.getEntries();

  // 1. Localizar o .sig
  const sigEntry = entries.find(e => e.entryName.endsWith('.sig'));
  if (!sigEntry) {
    return { ok: false, erros: ['Arquivo .sig não encontrado no ZIP'], payload: {} };
  }

  const doc: SigDoc = JSON.parse(sigEntry.getData().toString('utf-8'));
  const { payload, assinatura } = doc;

  // 2. JSON canônico do payload (sort_keys, sem espaços)
  const payloadJson = JSON.stringify(
    Object.fromEntries(Object.entries(payload).sort()),
    null, 0
  );
  // Atenção: JSON.stringify não garante sort recursivo em objetos aninhados.
  // Use a função abaixo para garantir compatibilidade com o Python sort_keys=True:
  const payloadJsonCanonical = canonicalJson(payload);

  // 3. Validar HMAC-SHA256
  const expectedSig = crypto
    .createHmac('sha256', Buffer.from(tokenCliente, 'utf-8'))
    .update(Buffer.from(payloadJsonCanonical, 'utf-8'))
    .digest('hex');

  if (!crypto.timingSafeEqual(Buffer.from(expectedSig), Buffer.from(assinatura))) {
    erros.push('Assinatura HMAC inválida — token não confere ou payload adulterado');
  }

  // 4. Validar hashes dos arquivos
  for (const entry of entries) {
    const name = entry.entryName;
    const data = entry.getData();

    if (name.endsWith('.txt')) {
      const h = sha256Buffer(data);
      if (h !== payload['hash_txt']) {
        erros.push(`Hash TXT diverge: esperado=${payload['hash_txt']} calculado=${h}`);
      }
    } else if (name.endsWith('.pdf') && payload['hash_pdf']) {
      const h = sha256Buffer(data);
      if (h !== payload['hash_pdf']) {
        erros.push(`Hash PDF diverge: esperado=${payload['hash_pdf']} calculado=${h}`);
      }
    } else if (name.startsWith('fotos/')) {
      const hashFotos = payload['hash_fotos'] as Record<string, string>;
      if (hashFotos && hashFotos[name]) {
        const h = sha256Buffer(data);
        if (h !== hashFotos[name]) {
          erros.push(`Hash foto diverge [${name}]: esperado=${hashFotos[name]} calculado=${h}`);
        }
      }
    }
  }

  return { ok: erros.length === 0, erros, payload };
}

/** Serializa objeto em JSON canônico com chaves ordenadas recursivamente */
function canonicalJson(obj: unknown): string {
  if (obj === null || typeof obj !== 'object' || Array.isArray(obj)) {
    return JSON.stringify(obj);
  }
  const sorted = Object.keys(obj as object)
    .sort()
    .map(k => `${JSON.stringify(k)}:${canonicalJson((obj as Record<string, unknown>)[k])}`)
    .join(',');
  return `{${sorted}}`;
}
```

---

## 6. Casos de erro e significado

| Erro | Causa provável |
|---|---|
| `Arquivo .sig não encontrado no ZIP` | ZIP corrompido, versão antiga do app, ou envio incompleto |
| `Assinatura HMAC inválida` | Token errado no banco, payload modificado em trânsito, ou serial incorreto no `.key` |
| `Hash TXT diverge` | Arquivo TXT substituído ou corrompido após a geração |
| `Hash PDF diverge` | Arquivo PDF substituído ou corrompido após a geração |
| `Hash foto diverge` | Foto substituída ou corrompida após a geração |
| `Foto declarada no .sig não encontrada no ZIP` | Arquivo removido do ZIP após a geração |

---

## 7. Fluxo recomendado para o manager

```
Receber ZIP via upload
        │
        ▼
  Extrair .sig do ZIP
        │
        ▼
  Buscar token do cliente
  (tabela clientes WHERE cnpj = payload.cnpj)
        │
        ▼
  Validar HMAC do .sig ──► FALHA → Rejeitar, registrar log de auditoria
        │
      SUCESSO
        │
        ▼
  Validar hashes dos arquivos ──► FALHA → Rejeitar, registrar log de auditoria
        │
      SUCESSO
        │
        ▼
  Processar contagem (importar TXT, armazenar PDF, etc.)
        │
        ▼
  Retornar 200 OK com url_arquivo
```

---

## 8. Notas adicionais

- **Tempo constante**: use sempre `hmac.compare_digest` (Python) ou `crypto.timingSafeEqual` (Node.js) para comparar assinaturas — evita ataques de timing.
- **Modo offline**: se o campo `serial` no payload estiver vazio, a assinatura é um SHA-256 simples do payload (sem HMAC). Neste caso, a autenticidade não pode ser verificada via token — apenas a integridade dos arquivos pode ser checada pelos hashes.
- **Campos futuros**: o payload pode receber novos campos em versões futuras do app. A validação deve usar apenas os campos conhecidos para recalcular o HMAC — ou seja, serializar o payload **completo** recebido no `.sig` (não reconstruí-lo manualmente).
