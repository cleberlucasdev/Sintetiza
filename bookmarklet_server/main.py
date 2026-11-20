import os
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Depends, dependencies
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

load_dotenv(override=True)
PASSKEY = os.getenv("PASSKEY")
CODE_SNIPPET = os.getenv("CODE_SNIPPET")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://www.chatmix.com.br"],
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

def auth(authorization: HTTPAuthorizationCredentials = Depends(HTTPBearer())):
    if not authorization.credentials == PASSKEY:
        raise HTTPException(status_code=401, detail="Não autorizado! Verifique se a chave da API está correta.")

@app.get('/', dependencies=[Depends(auth)])
def return_snippet():
    return {"code":f"{CODE_SNIPPET}"}
