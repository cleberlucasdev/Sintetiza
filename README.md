# 📋 Report-Maker

> 🇧🇷 [Português](#português) | 🇺🇸 [English](#english)

---

<a name="português"></a>
## 🇧🇷 Português

Gerador automático de relatórios de atendimento com IA.

### O que faz

Atendentes de suporte lidam com dezenas de chamados por dia — mensagens de texto, áudios, interações com bot. Escrever um relatório para cada atendimento manualmente é repetitivo, lento e sujeito a erros.

**O Report-Maker elimina isso.**

Com um único clique, o atendente aciona um bookmarklet no browser que lê todo o histórico do chat, envia para um backend que transcreve automaticamente os áudios e gera um relatório limpo e conciso — pronto para copiar e colar.

**Antes:** O atendente lê o chat inteiro e escreve o resumo manualmente. Leva 2 a 5 minutos por chamado.

**Depois:** Um clique. O relatório aparece em segundos.

---

### Como funciona

```
Atendente abre o chat → clica no bookmarklet
        ↓
Bookmarklet extrai todas as mensagens e URLs de áudio do DOM da página
        ↓
Envia tudo para o backend via HTTP POST
        ↓
Backend baixa cada arquivo de áudio e transcreve (Groq Whisper)
        ↓
Texto completo da conversa é enviado para IA (Google Gemini)
        ↓
Relatório gerado e retornado ao atendente
```

---

### Stack

| Camada | Tecnologia | Função |
|---|---|---|
| Browser | JavaScript puro (Bookmarklet) | Extração do DOM, sem instalação |
| Backend | Python + FastAPI | Servidor de API e orquestração |
| Transcrição | Groq Whisper large-v3 | Áudio → texto |
| Geração de relatório | Google Gemini 1.5 Flash | Sumarização de texto |
| Hospedagem | Render | Free tier, sempre ativo |

---

### Decisões técnicas

**Bookmarklet em vez de extensão** — zero instalação para o usuário final. Funciona em qualquer browser baseado em Chromium. Sem processo de revisão, sem fricção de distribuição.

**Scraping do DOM em vez de integração via API** — a plataforma alvo (Chatmix) não expõe API pública com histórico do chat. O bookmarklet roda no contexto da página com acesso total ao DOM, contornando essa limitação.

**Groq para transcrição** — mesma qualidade do Whisper da OpenAI, tier gratuito, inferência significativamente mais rápida. Sem custo para uso típico de equipe de suporte.

**Redação de CPF/CNPJ antes da transmissão** — dados fiscais do cliente são removidos do payload via regex no lado do cliente, antes de qualquer dado sair do browser. Conformidade por design.

---

### Privacidade e segurança

- CPF e CNPJ são removidos antes de sair do browser
- Nenhum dado do chat é armazenado — o backend processa e descarta
- Arquivos de áudio são baixados, transcritos e deletados da memória imediatamente

---

### Variáveis de ambiente

```
GROQ_API_KEY=sua_chave_groq
GEMINI_API_KEY=sua_chave_gemini
```

---

### API

**`POST /generate-report`**

Request:
```json
{ "chat_log": "texto completo extraído do chat" }
```

Response:
```json
{ "report": "Cliente relatou lentidão no celular e iPad..." }
```

**`GET /health`**
```json
{ "status": "ok" }
```

---

### Impacto real

Construído e implantado na **Tríade Fibra**, provedor brasileiro de internet FTTH. Reduziu o tempo de escrita de relatórios de ~3 minutos para menos de 10 segundos por atendimento.

---

<a name="english"></a>
## 🇺🇸 English

Automated AI-powered attendance report generator for customer support teams.

### What it does

Customer support attendants deal with dozens of calls per day — text messages, voice notes, bot interactions. Writing a report for each one manually is repetitive, slow, and error-prone.

**Report-Maker eliminates that.**

With a single click, the attendant triggers a browser bookmarklet that reads the entire chat history, sends it to a backend, which automatically transcribes any voice notes and generates a clean, concise report — ready to copy and paste.

**Before:** Attendant reads the whole chat, writes a summary manually. Takes 2–5 minutes per call.

**After:** One click. Report appears in seconds.

---

### How it works

```
Attendant opens the chat → clicks the bookmarklet
        ↓
Bookmarklet extracts all messages and audio URLs from the page DOM
        ↓
Sends everything to the backend via HTTP POST
        ↓
Backend downloads each audio file and transcribes it (Groq Whisper)
        ↓
Full conversation text is sent to an AI model (Google Gemini)
        ↓
Report is generated and returned to the attendant
```

---

### Tech stack

| Layer | Technology | Purpose |
|---|---|---|
| Browser | Vanilla JavaScript (Bookmarklet) | DOM extraction, no install needed |
| Backend | Python + FastAPI | API server, orchestration |
| Transcription | Groq Whisper large-v3 | Voice note → text |
| Report generation | Google Gemini 1.5 Flash | Text summarization |
| Hosting | Render | Free tier, always-on |

---

### Key engineering decisions

**Bookmarklet instead of browser extension** — zero installation for the end user. Works on any Chromium-based browser. No review process, no distribution friction.

**DOM scraping instead of API integration** — the target platform (Chatmix) does not expose a public API with chat history. The bookmarklet runs in the page context with full DOM access, bypassing that limitation entirely.

**Groq for transcription** — same model quality as OpenAI Whisper, free tier, significantly faster inference. No cost for typical support team usage.

**CPF/CNPJ redaction before transmission** — customer tax IDs are stripped from the payload client-side via regex before any data leaves the browser. Compliance-first by design.

---

### Privacy & security

- Customer tax IDs (CPF/CNPJ) are redacted before leaving the browser
- No chat data is stored — the backend processes and discards
- Audio files are downloaded, transcribed, and deleted from memory immediately

---

### Environment variables

```
GROQ_API_KEY=your_groq_key
GEMINI_API_KEY=your_gemini_key
```

---

### API

**`POST /generate-report`**

Request:
```json
{ "chat_log": "full extracted chat text" }
```

Response:
```json
{ "report": "Cliente relatou lentidão no celular e iPad..." }
```

**`GET /health`**
```json
{ "status": "ok" }
```

---

### Real-world impact

Built for and deployed at **Tríade Fibra**, a Brazilian FTTH internet provider. Reduced report writing time from ~3 minutes to under 10 seconds per attendance.
