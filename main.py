import os
import re
import httpx
import tempfile
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

REPORT_PROMPT = ""Você é um analista técnico de ISP especializado em diagnóstico de falhas FTTH.

Analise o histórico e gere um relatório técnico em UM ÚNICO PARÁGRAFO.

REGRAS:

- Não narre o atendimento. Extraia apenas informação útil.
- Sempre identifique a CAUSA MAIS PROVÁVEL (obrigatório).
- Se houver causas secundárias, mencione brevemente.
- Inclua evidências objetivas (ex: teste via cabo, nível de sinal, quedas PPPoE).
- Não use linguagem vaga ("pode ser") sem priorização.
- Seja direto, técnico e curto.

ESTRUTURA (dentro do parágrafo):

[Sintoma] + [Evidências] + [Diagnóstico principal] + [Ação tomada]

REGRAS TÉCNICAS:

- Se cabo estável → Wi-Fi não é causa principal
- Se sinal <= -25 dBm → priorizar problema físico
- Quedas PPPoE → indicar instabilidade de link

EXEMPLO IDEAL:

"Cliente relatou instabilidade com quedas frequentes. Verificado ONU online com quedas PPPoE e sinal em -26 dBm. Teste via cabo estável, descartando rede interna. Diagnóstico indica degradação de sinal óptico como causa principal, com possível influência de roteador próprio. Ajuste remoto realizado e agendada visita técnica para validação do cabo drop e conectores."

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


async def generate_with_groq(prompt: str) -> str:
    payload = {
        "model": "llama-3.3-70b-versatile",
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
