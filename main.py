import os
import re
import httpx
import tempfile
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import google.generativeai as genai

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST"],
    allow_headers=["*"],
)

GROQ_API_KEY = os.environ["GROQ_API_KEY"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

genai.configure(api_key=GEMINI_API_KEY)
gemini = genai.GenerativeModel("gemini-1.5-flash")

AUDIO_PATTERN = re.compile(r'\[AUDIO: (https?://\S+?)\](?:\n\(No transcription found\))?')

REPORT_PROMPT = """Você é um assistente especializado em gerar relatórios de atendimento técnico para um provedor de internet (ISP).

Analise o histórico de atendimento abaixo e gere um relatório conciso em português, em um único parágrafo.

Regras:
- Ignore mensagens de sistema, menus do bot e transferências
- Foque apenas no problema relatado, diagnóstico e resolução
- Não mencione nomes de atendentes, apenas "o Suporte"
- Não mencione protocolos, horários nem CPF
- Seja direto e técnico, como nos exemplos abaixo

Exemplos de relatório bem feito:
"Cliente relatou lentidão no celular e iPad, sem conseguir abrir sites. Realizados ajustes no roteador e reinicialização. Orientada a migrar para a rede 5 GHz. Cliente confirmou normalização."
"Cliente solicitou religação após regularização de débito. Verificado desbloqueio já realizado via promessa de pagamento. Cliente confirmou funcionamento normal da conexão."
"Foi feita uma visita na residência deste cliente. O técnico sugeriu a instalação de um ponto adicional para melhorar a conectividade, mas cliente negou. Hoje, entrou em contato relatando o mesmo problema. Foi repassado ao cliente o que o técnico informou no dia da visita."

Histórico:
{chat_log}

Relatório:"""


class ReportRequest(BaseModel):
    chat_log: str


async def transcribe_audio(url: str) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        audio_response = await client.get(url)
        if audio_response.status_code != 200:
            return "[transcrição indisponível]"

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            f.write(audio_response.content)
            tmp_path = f.name

        with open(tmp_path, "rb") as audio_file:
            response = await client.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                files={"file": ("audio.ogg", audio_file, "audio/ogg")},
                data={"model": "whisper-large-v3", "language": "pt"},
            )

        os.unlink(tmp_path)

        if response.status_code != 200:
            return "[transcrição indisponível]"

        return response.json().get("text", "[transcrição indisponível]")


async def process_chat_log(chat_log: str) -> str:
    audio_urls = AUDIO_PATTERN.findall(chat_log)

    for url in audio_urls:
        transcription = await transcribe_audio(url)
        chat_log = AUDIO_PATTERN.sub(
            f"[ÁUDIO TRANSCRITO: {transcription}]",
            chat_log,
            count=1
        )

    return chat_log


@app.post("/generate-report")
async def generate_report(request: ReportRequest):
    if not request.chat_log.strip():
        raise HTTPException(status_code=400, detail="chat_log vazio")

    processed_log = await process_chat_log(request.chat_log)

    prompt = REPORT_PROMPT.format(chat_log=processed_log)
    response = gemini.generate_content(prompt)
    report = response.text.strip()

    return {"report": report}


@app.get("/health")
async def health():
    return {"status": "ok"}
