from fastapi import FastAPI
from random import randint

app = FastAPI()

@app.get("/")
async def root():
    return {"message": "Hello World"}

@app.get("/random/{n}")
async def api_random(n: int):
    return { "size": n, "data":"".join(str(randint(0,9)) for _ in range(n))}
