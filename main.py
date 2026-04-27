import os
import re
import httpx
import tempfile
import asyncio
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST"],
    allow_headers=["*"],
)

GROQ_API_KEY = os.environ["GROQ_API_KEY"]

AUDIO_PATTERN = re.compile(r'\[AUDIO: (https?://\S+?)\](?:\n\(No transcription found\))?')

REPORT_PROMPT = """Você é um escriba de atendimentos de suporte técnico. Sua única função é resumir o que foi dito no chat, sem inventar, diagnosticar, propor soluções ou presumir nada além do que está explicitamente registrado.

Regras:
- Não use gírias nem expressões informais
- Não cometa erros ou desvios gramaticais
- A escrita deve ser técnica e formal: o padrão esperado de um relatório para uma empresa
- Resuma apenas o que foi dito — nada mais
- Não diagnostique, não proponha ações, não faça perguntas
- Se o atendimento foi curto ou inconclusivo, o relatório também será curto
- Ignore mensagens de sistema, menus do bot e transferências
- Não mencione nomes de atendentes
- Não mencione protocolos, horários, nem dados pessoais (como nome completo)
- Escreva em um único parágrafo, em português

Exemplos:

Chat: cliente disse "sem internet", sem resposta do suporte.
Relatório: "Cliente entrou em contato relatando ausência de internet. Atendimento sem resposta registrada."

Chat: cliente pediu troca de senha do Wi-Fi, suporte realizou a alteração, cliente confirmou.
Relatório: "Cliente entrou em contato solicitando alteração da senha do Wi-Fi. Alteração realizada conforme solicitado e cliente confirmou funcionamento. Atendimento finalizado com sucesso."

Chat: cliente relatou lentidão, suporte identificou débito em aberto causando redução de velocidade, orientou pagamento, cliente não respondeu mais.
Relatório: "Cliente entrou em contato relatando lentidão na conexão. Foi identificado débito em aberto, ocasionando redução de velocidade pelo sistema. Cliente orientada a realizar pagamento e enviar comprovante para normalização. Atendimento encerrado por ausência de resposta da cliente."

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
    if not audio_urls:
        return chat_log
    
    transcriptions = await asyncio.gather(*[transcribe_audio(url) for url in audio_urls])
    
    for transcription in transcriptions:
        chat_log = AUDIO_PATTERN.sub(
            f"[ÁUDIO TRANSCRITO: {transcription}]",
            chat_log,
            count=1
        )
    return chat_log


async def generate_with_groq(prompt: str) -> str:
    payload = {
        "model": "openai/gpt-oss-120b",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1000,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json=payload,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"].strip()


@app.post("/generate-report")
async def generate_report(request: ReportRequest):
    if not request.chat_log.strip():
        raise HTTPException(status_code=400, detail="chat_log vazio")

    processed_log = await process_chat_log(request.chat_log)
    prompt = REPORT_PROMPT.format(chat_log=processed_log)
    report = await generate_with_groq(prompt)
    return {"report": report}


@app.get("/health")
async def health():
    return {"status": "ok"}
