"""
drafting/main.py
──────────────────
GD (General Diary) / FIR drafting service.
- Officer provides rough notes (voice transcript or typed text)
- LLM structures the draft into official format
- Detects missing required fields
- Extracts named entities (persons, vehicles, locations, times)
- Supports Bangla + English
- Officer must approve before any draft is saved as final
- Every draft and approval is audit-logged
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone

import asyncpg
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

log = logging.getLogger("drafting")
logging.basicConfig(level=logging.INFO)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://policeai:policeai_dev_secret@localhost:5432/policeai")
LLM_API_URL  = os.getenv("LLM_API_URL", "http://localhost:11434")   # Ollama local LLM
LLM_MODEL    = os.getenv("LLM_MODEL",   "llama3.1:8b")              # or gemma3, mistral

app = FastAPI(title="Police AI – GD/FIR Drafting Service")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

pool: asyncpg.Pool | None = None


@app.on_event("startup")
async def startup():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)


# ── GD required fields (Bangladesh Police format) ────
GD_REQUIRED_FIELDS = [
    "complainant_name",
    "complainant_address",
    "complainant_contact",
    "incident_date",
    "incident_time",
    "incident_location",
    "incident_description",
    "accused_details",        # may be 'unknown'
    "witness_details",        # may be 'none'
    "property_involved",      # may be 'none'
    "prayer",                 # what complainant requests
]

FIR_REQUIRED_FIELDS = GD_REQUIRED_FIELDS + [
    "sections_invoked",       # penal code sections
    "investigation_officer",
    "station_name",
]

# ── LLM prompt templates ──────────────────────
GD_SYSTEM_PROMPT = """You are a professional police documentation assistant for Bangladesh Police.
Your task is to convert rough officer notes into a structured General Diary (GD) entry.

Output ONLY valid JSON with this exact structure:
{
  "draft_text": "<formatted GD text in Bangla>",
  "structured_fields": {
    "complainant_name": "",
    "complainant_address": "",
    "complainant_contact": "",
    "incident_date": "",
    "incident_time": "",
    "incident_location": "",
    "incident_description": "",
    "accused_details": "",
    "witness_details": "",
    "property_involved": "",
    "prayer": ""
  },
  "entities_extracted": {
    "persons": [],
    "vehicles": [],
    "locations": [],
    "phone_numbers": [],
    "nid_numbers": []
  },
  "missing_fields": [],
  "language_detected": "bn or en",
  "confidence_score": 0.0
}

Rules:
- For missing information, set the field to null (do NOT invent information)
- List field names with null values in "missing_fields"
- Draft text should be formal Bangla police language
- Extract all mentioned names, vehicles, addresses as entities
- Never add facts not present in the officer's notes
- confidence_score: 0-1, how complete the draft is based on available info
"""

FIR_SYSTEM_PROMPT = GD_SYSTEM_PROMPT.replace(
    "General Diary (GD)", "First Information Report (FIR)"
).replace("GD text", "FIR text")


# ── Pydantic models ────────────────────────────
class DraftRequest(BaseModel):
    draft_type: str           # "GD" or "FIR"
    raw_notes: str            # Officer's rough notes
    language: str = "bn"      # "bn" = Bangla, "en" = English
    incident_id: str | None = None
    officer_id: str | None = None


class DraftApproval(BaseModel):
    officer_id: str
    approved: bool
    edits: str | None = None  # Officer's manual edits to the draft


# ── LLM client ────────────────────────────────
async def call_llm(system_prompt: str, user_content: str) -> dict:
    """
    Call local LLM (Ollama) or OpenAI-compatible API.
    Returns parsed JSON response.

    For production with Bangla: use a multilingual model:
    - google/gemma-3-12b (good Bangla support)
    - meta-llama/Llama-3.1-8B-Instruct
    - For better Bangla: fine-tune on banglabert base with police corpus
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{LLM_API_URL}/api/chat",
                json={
                    "model": LLM_MODEL,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": user_content},
                    ],
                    "stream": False,
                    "format": "json",
                    "options": {"temperature": 0.1},   # Low temp for structured output
                }
            )
            data     = resp.json()
            content  = data["message"]["content"]
            return json.loads(content)

    except Exception as e:
        log.error(f"LLM call failed: {e}")
        # Return empty structure on failure
        return {
            "draft_text": f"[LLM unavailable — raw notes preserved]\n\n{user_content}",
            "structured_fields": {f: None for f in GD_REQUIRED_FIELDS},
            "entities_extracted": {"persons": [], "vehicles": [],
                                    "locations": [], "phone_numbers": [],
                                    "nid_numbers": []},
            "missing_fields":  GD_REQUIRED_FIELDS,
            "language_detected": "unknown",
            "confidence_score": 0.0,
        }


def _officer_uuid(value: str | None):
    if not value:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


@app.get("/")
async def root():
    return {"service": "drafting", "endpoints": {"draft": "POST /draft", "health": "/health"}}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "drafting",
            "llm_model": LLM_MODEL}


@app.post("/draft")
async def create_draft(req: DraftRequest):
    """
    Main endpoint: convert officer notes → structured draft.
    Returns draft for officer review — NOT yet saved as approved.
    """
    if req.draft_type not in ("GD", "FIR"):
        raise HTTPException(400, "draft_type must be GD or FIR")

    system_prompt = GD_SYSTEM_PROMPT if req.draft_type == "GD" else FIR_SYSTEM_PROMPT

    # Inject language instruction
    user_content = f"""
[Language: {'Bangla' if req.language == 'bn' else 'English'}]
[Draft type: {req.draft_type}]

Officer's notes:
{req.raw_notes}

Convert these notes into a structured {req.draft_type} entry.
"""

    log.info(f"Generating {req.draft_type} draft — "
             f"notes length={len(req.raw_notes)} chars")

    result = await call_llm(system_prompt, user_content)

    # Validate missing fields
    required = GD_REQUIRED_FIELDS if req.draft_type == "GD" else FIR_REQUIRED_FIELDS
    structured = result.get("structured_fields", {})
    missing = [f for f in required if not structured.get(f)]
    result["missing_fields"] = missing

    # Save as pending draft in DB
    officer_uuid = _officer_uuid(req.officer_id)

    draft_id = await pool.fetchval(
        """INSERT INTO drafts
           (draft_type, incident_id, officer_id, raw_notes, structured_json,
            missing_fields, draft_text, language, status)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'draft')
           RETURNING id""",
        req.draft_type,
        uuid.UUID(req.incident_id) if req.incident_id else None,
        officer_uuid,
        req.raw_notes,
        json.dumps(result.get("structured_fields", {})),
        missing,
        result.get("draft_text", ""),
        req.language
    )

    # Audit log
    await pool.execute(
        """INSERT INTO audit_log (officer_id, action, resource_type, resource_id, details)
           VALUES ($1, 'draft_created', 'draft', $2, $3)""",
        officer_uuid,
        draft_id,
        json.dumps({"draft_type": req.draft_type,
                    "missing_fields_count": len(missing)})
    )

    return {
        "draft_id":          str(draft_id),
        "draft_type":        req.draft_type,
        "draft_text":        result.get("draft_text", ""),
        "structured_fields": result.get("structured_fields", {}),
        "missing_fields":    missing,
        "entities_extracted": result.get("entities_extracted", {}),
        "confidence_score":  result.get("confidence_score", 0.0),
        "status":            "pending_review",
        "message":           (
            f"Draft ready. {len(missing)} field(s) need attention. "
            "Please review, edit if needed, then approve."
        )
    }


@app.post("/draft/{draft_id}/approve")
async def approve_draft(draft_id: str, body: DraftApproval):
    """
    Officer approves (or rejects) a draft.
    This is the mandatory human-in-the-loop gate.
    Approved drafts are locked — no further AI modification.
    """
    draft = await pool.fetchrow(
        "SELECT * FROM drafts WHERE id=$1", uuid.UUID(draft_id)
    )
    if not draft:
        raise HTTPException(404, "Draft not found")

    if draft["status"] in ("approved", "submitted"):
        raise HTTPException(400, "Draft already approved")

    new_status = "approved" if body.approved else "draft"
    final_text = body.edits or draft["draft_text"]

    off_uuid = _officer_uuid(body.officer_id)

    await pool.execute(
        """UPDATE drafts
           SET status=$1, draft_text=$2, approved_by=$3, approved_at=NOW(),
               updated_at=NOW()
           WHERE id=$4""",
        new_status,
        final_text,
        off_uuid,
        uuid.UUID(draft_id)
    )

    await pool.execute(
        """INSERT INTO audit_log
           (officer_id, action, resource_type, resource_id, details)
           VALUES ($1, $2, 'draft', $3, $4)""",
        off_uuid,
        "draft_approved" if body.approved else "draft_returned",
        uuid.UUID(draft_id),
        json.dumps({"approved": body.approved,
                    "had_edits": body.edits is not None})
    )

    log.info(f"Draft {draft_id} {'approved' if body.approved else 'returned'} "
             f"by officer {body.officer_id}")

    return {
        "draft_id": draft_id,
        "status":   new_status,
        "message":  "Draft approved and locked." if body.approved
                    else "Draft returned for revision."
    }


@app.get("/drafts")
async def list_drafts(officer_id: str | None = None,
                       status: str | None = None,
                       limit: int = 20):
    query = "SELECT id, draft_type, status, missing_fields, created_at FROM drafts"
    params = []
    conditions = []
    if officer_id:
        conditions.append(f"officer_id=${len(params)+1}")
        params.append(uuid.UUID(officer_id))
    if status:
        conditions.append(f"status=${len(params)+1}")
        params.append(status)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += f" ORDER BY created_at DESC LIMIT ${len(params)+1}"
    params.append(limit)
    rows = await pool.fetch(query, *params)
    return [dict(r) for r in rows]


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8006, reload=True)
