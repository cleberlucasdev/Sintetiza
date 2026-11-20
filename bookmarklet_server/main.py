import os
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Depends, dependencies
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.middleware.cors import CORSMiddleware

load_dotenv(override=True)
PASSKEY = os.getenv("PASSKEY")
CODE_SNIPPET = (Path(__file__).parent / "snippet.js").read_text(encoding="utf-8")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://www.chatmix.com.br"],
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

def auth(authorization: HTTPAuthorizationCredentials = Depends(HTTPBearer())):
    if not authorization.credentials == PASSKEY:
        raise HTTPException(status_code=401, detail=f"Não autorizado! Verifique se a chave da API está correta.")

@app.get('/', dependencies=[Depends(auth)])
def return_snippet():
    return {"code":f"{CODE_SNIPPET}"}
