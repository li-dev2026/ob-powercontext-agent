from fastapi import FastAPI
from pydantic import BaseModel
from agent.context_retrieval import handle_query

app = FastAPI()

class RequestBody(BaseModel):
    query: str
    context: list
    history: list

@app.post("/run")
async def run_task(req: RequestBody):
    resp = handle_query(req.query, req.context, req.history)
    return {"result": resp}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000)
