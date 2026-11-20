import os
import re
import httpx
import tempfile
import asyncio
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel
from starlette.middleware.exceptions import ExceptionMiddleware
from fastapi.responses import JSONResponse

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

@app.exception_handler(Exception)
async def cors_safe_exception_handler(request, exc):
    return JSONResponse(
        status_code=500,
        content={"detail": str(exc)},
        headers={"Access-Control-Allow-Origin": "*"},
    )

GROQ_API_KEY = os.environ["GROQ_API_KEY"]

AUDIO_PATTERN = re.compile(r'\[AUDIO: (https?://\S+?)\](?:\n\(No transcription found\))?')

REPORT_PROMPT = """Resuma este atendimento de suporte técnico num registro curto. É o resultado do atendimento do Suporte Técnico, não a história de como se chegou a ele.
Você receberá, além deste prompt, três arquivos: 
1) "Chatlog": é a fonte principal, com todos os acontecimentos do atendimento.
2) "Examples": exemplos de resumos de atendimentos, você deve se basear nestes ao escrever.
3) "Additional Information": informações adicionais sobre o atendimento: É o que foi tecnicamente feito de verdade, não é fala do cliente, e tem prioridade sobre o chat quando existir. Se estiver vazio, ignore.

O foco do relatório é um resumo geral dos acontecimentos, de forma que o desfecho seja devidamente registrado.

Inclua apenas, num parágrafo corrido, o seguinte:
- o que o cliente relatou,
- o que foi feito tecnicamente,
- somente o atendimento do Suporte Técnico: ignore mensagens de outros setores, como Financeiro e Comercial, e não inclua seu desfecho.
- como terminou.

Nunca adicione uma frase separada, no final, resumindo ou rotulando o desfecho (ex: nada de "Desfecho: ...", "Resultado: ...", ou qualquer frase que apenas repita o que já foi dito na frase anterior com outras palavras).

Nunca inclua:
- O caminho até a causa: testes que não deram resultado, erro de digitação do cliente, confirmações redundantes da mesma situação já descrita.
- Logística do atendimento: troca de atendente, turno, protocolo, nomes, horários, dados pessoais.
- Informações específicas que não fazem sentido ser documentadas (o foco do relatório é um resumo geral dos acontecimentos). Ex: senha e SSID de roteador, nome completo da cliente, horário de atendimento, etc.
- Cortesia (agradecimento, despedida).

Um parágrafo, português, tom impessoal ("foi feito", nunca "o agente fez"). O mais curto possível sem perder fato real. Se uma frase pode ser cortada sem perder informação, corte. Se faltar informação, diga isso em poucas palavras e pare; não invente.
"""

EXAMPLES = """
Exemplos:
1) "Cliente relatou lentidão apenas no Wi-Fi, com conexão cabeada normal. 
Identificada interferência de canal na faixa de 2,4 GHz, com outras redes concentradas no mesmo canal. Canal foi alterado para faixa menos congestionada, e cliente confirmou velocidade normalizada."
2) "Cliente relatou quedas de conexão a cada poucas horas. Verificado sinal óptico abaixo do esperado na ONU, causado por conector de fibra sujo no ponto de terminação. Conector foi limpo e o sinal retornou aos níveis normais, cliente confirmou estabilidade."
3) "Cliente relatou falta de internet, com equipamento ligado e luz de conexão acesa. 
Constatado que o login PPPoE estava desprovisionado após troca de equipamento recente. Credenciais foram reprovisionadas e a conexão restabelecida, e o cliente confirmou acesso."
"""

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


async def generate_with_groq(prompt: str, processed_log: str, examples: str, additional_info: str) -> str:
    payload = {
        "model": "openai/gpt-oss-120b",
        "messages": [{"role": "system", "content": prompt}, 
                     {"role": "user", "content": processed_log},
                     {"role": "system", "content": examples},
                     {"role": "user", "content": additional_info}
                    ],
        "max_tokens": 6500,
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

    processed_log = "Chatlog:\n" + await process_chat_log(request.chat_log)

    info = request.additional_info.strip()
    additional_info = f"Additional Information:\n {info}" if info else "(nenhuma)"

    report = await generate_with_groq(REPORT_PROMPT, processed_log, EXAMPLES, additional_info)
    return {"report": report}


@app.get("/health")
async def health():
    return {"status": "ok"}
