import os
import re
import httpx
import tempfile
import asyncio
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.head("/health")
async def health_head():
    return Response(status_code=200)

GROQ_API_KEY = os.environ["GROQ_API_KEY"]

AUDIO_PATTERN = re.compile(r'\[AUDIO: (https?://\S+?)\](?:\n\(No transcription found\))?')

REPORT_PROMPT = """Resuma este atendimento de suporte técnico num registro curto para consulta futura sobre esse cliente. É o resultado do atendimento, não a história de como se chegou a ele.

Inclua apenas, integrado num parágrafo corrido — nunca como rótulo, campo ou lista:
- o que o cliente relatou,
- o que foi feito tecnicamente,
- como terminou: resolvido, não resolvido, ou não confirmado (com o motivo, em poucas palavras).

O desfecho já deve estar dentro da narrativa. NUNCA adicione uma frase separada no final resumindo ou rotulando o desfecho (ex: nada de "Desfecho: ...", "Resultado: ...", ou qualquer frase que apenas repita o que já foi dito na frase anterior com outras palavras).

Nunca inclua:
- O caminho até a causa: testes que não deram resultado, erro de digitação do cliente, confirmações redundantes do mesmo fix já relatado.
- Logística do atendimento: troca de atendente, turno, protocolo, nomes, horários, dados pessoais.
- Orientação automática padrão que não foi o que resolveu o problema.
- Cortesia (agradecimento, despedida).

Um parágrafo, português, tom impessoal ("foi feito", nunca "o agente fez"). O mais curto possível sem perder fato real — se uma frase pode ser cortada sem perder informação, corte. Se faltar informação, diga isso em poucas palavras e pare; não invente.

"Informações adicionais" (quando houver) é o que foi tecnicamente feito de verdade, não é fala do cliente, e tem prioridade sobre o chat.

Exemplos:
1) "Cliente relatou lentidão na conexão. Realizados ajustes no roteador. Cliente confirmou normalização."

2) "Cliente relatou instabilidade intermitente há 3 dias. Identificado sinal óptico baixo na ONU e trocado o cabo drop; firmware do roteador foi atualizado. Cliente confirmou estabilidade."

3) "Cliente relatou lentidão na rede e solicitou troca de senha do aplicativo. Foi feita atualização no roteador; cliente não pôde testar por não estar em casa no momento. Acesso ao aplicativo restaurado."

Histórico do chat:
{chat_log}

Informações adicionais (interno, pode não ter sido dito ao cliente):
{additional_info}

Relatório:"""

class ReportRequest(BaseModel):
    chat_log: str
    additional_info: str = ""


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
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 350,
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
    additional_info = request.additional_info.strip() or "(nenhuma)"
    prompt = REPORT_PROMPT.format(
        chat_log=processed_log,
        additional_info=additional_info,
    )
    report = await generate_with_groq(prompt)
    return {"report": report}


@app.get("/health")
async def health():
    return {"status": "ok"}
