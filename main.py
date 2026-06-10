"""
Smartlead Lead Manager
======================
Single-file Python app — no Node.js, no npm, no build step.

Run:
    python app.py

Then open:  http://localhost
"""

import io
import os
import logging
from typing import Optional

import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Smartlead Lead Manager API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ====================================================================
# Smartlead Service (inline)
# ====================================================================
import httpx

BASE_URL = "https://server.smartlead.ai/api/v1"


class SmartleadClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.client = httpx.AsyncClient(timeout=60.0)

    async def close(self):
        await self.client.aclose()

    def _params(self, extra: Optional[dict] = None) -> dict:
        params = {"api_key": self.api_key}
        if extra:
            params.update(extra)
        return params

    async def list_campaigns(self) -> list[dict]:
        resp = await self.client.get(f"{BASE_URL}/campaigns/", params=self._params())
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else data.get("data", [])

    async def get_campaign(self, campaign_id: int) -> dict:
        resp = await self.client.get(
            f"{BASE_URL}/campaigns/{campaign_id}", params=self._params()
        )
        resp.raise_for_status()
        return resp.json()

    async def get_lead_by_email(self, email: str) -> Optional[dict]:
        resp = await self.client.get(
            f"{BASE_URL}/leads/", params=self._params({"email": email})
        )
        resp.raise_for_status()
        data = resp.json()
        if not data or not data.get("id"):
            return None
        return data

    async def add_leads_to_campaign(
        self, campaign_id: int, lead_list: list[dict], settings: Optional[dict] = None
    ) -> dict:
        payload = {"lead_list": lead_list}
        if settings:
            payload["settings"] = settings
        resp = await self.client.post(
            f"{BASE_URL}/campaigns/{campaign_id}/leads",
            params=self._params(),
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()

    async def fetch_lead_categories(self) -> list[dict]:
        resp = await self.client.get(
            f"{BASE_URL}/leads/fetch-categories", params=self._params()
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else data.get("data", [])

    async def update_lead_category(
        self, campaign_id: int, lead_id: int, category_id: int
    ) -> dict:
        resp = await self.client.post(
            f"{BASE_URL}/campaigns/{campaign_id}/leads/{lead_id}",
            params=self._params(),
            json={"lead_category_id": category_id},
        )
        resp.raise_for_status()
        return resp.json()


# Per-request client (reads API key from env or header)
def get_client(api_key: str) -> SmartleadClient:
    if not api_key:
        raise HTTPException(
            status_code=401,
            detail="No API key provided. Pass X-API-Key header or set SMARTLEAD_API_KEY in .env",
        )
    return SmartleadClient(api_key)


def resolve_api_key(request: Request) -> str:
    """Resolve API key: header > env."""
    return (
        request.headers.get("X-API-Key", "")
        or os.environ.get("SMARTLEAD_API_KEY", "")
    )


# ====================================================================
# Pydantic Models
# ====================================================================
class LeadResult(BaseModel):
    email: str
    lead_id: Optional[int] = None
    status: str
    campaigns_count: int = 0
    message: str = ""


class ClassifyResponse(BaseModel):
    campaign_id: int
    campaign_name: str
    total_processed: int
    successful: int
    failed: int
    results: list[LeadResult]


class LeadCampaignInfo(BaseModel):
    campaign_id: int
    campaign_name: str
    campaign_lead_map_id: int
    lead_category_id: Optional[int] = None


class LeadLookupResponse(BaseModel):
    email: str
    lead_id: Optional[int] = None
    found: bool
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    company_name: Optional[str] = None
    campaigns_count: int = 0
    campaigns: list[LeadCampaignInfo] = []


# ====================================================================
# API Endpoints
# ====================================================================
@app.get("/api/health")
async def health_check(request: Request):
    api_key = resolve_api_key(request)
    if api_key:
        try:
            client = get_client(api_key)
            campaigns = await client.list_campaigns()
            await client.close()
            return {
                "status": "ok",
                "api_key_configured": True,
                "campaigns_count": len(campaigns),
            }
        except Exception as e:
            return {"status": "error", "api_key_configured": True, "detail": str(e)}
    return {"status": "ok", "api_key_configured": False}


@app.get("/api/campaigns")
async def list_campaigns(request: Request):
    api_key = resolve_api_key(request)
    client = get_client(api_key)
    try:
        campaigns = await client.list_campaigns()
        return {"campaigns": campaigns}
    except Exception as e:
        logger.error(f"Failed to fetch campaigns: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await client.close()


@app.post("/api/upload-and-classify", response_model=ClassifyResponse)
async def upload_and_classify(
    request: Request,
    file: UploadFile = File(...),
    campaign_id: int = Form(...),
):
    api_key = resolve_api_key(request)
    client = get_client(api_key)

    if not file.filename:
        raise HTTPException(status_code=400, detail="No file uploaded")

    filename = file.filename.lower()
    try:
        contents = await file.read()
        if filename.endswith(".csv"):
            df = pd.read_csv(io.BytesIO(contents))
        elif filename.endswith((".xlsx", ".xls")):
            df = pd.read_excel(io.BytesIO(contents))
        else:
            raise HTTPException(
                status_code=400, detail="Unsupported format. Use .csv or .xlsx"
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to parse file: {e}")
    finally:
        await file.close()

    # Normalise column names
    df.columns = [c.strip().lower() for c in df.columns]

    if "email" not in df.columns:
        raise HTTPException(status_code=400, detail="File must contain an 'email' column")

    has_category = "lead_category" in df.columns

    try:
        campaign = await client.get_campaign(campaign_id)
        campaign_name = campaign.get("name", f"Campaign {campaign_id}")
    except Exception:
        campaign_name = f"Campaign {campaign_id}"

    results: list[LeadResult] = []
    successful = 0
    failed = 0

    for _, row in df.iterrows():
        email = str(row["email"]).strip() if pd.notna(row["email"]) else ""
        if not email or "@" not in email:
            results.append(
                LeadResult(
                    email=email or "<empty>",
                    status="error",
                    message="Invalid email address",
                )
            )
            failed += 1
            continue

        category = ""
        if has_category and pd.notna(row.get("lead_category", "")):
            category = str(row["lead_category"]).strip()

        try:
            existing_lead = await client.get_lead_by_email(email)

            lead_data: dict = {"email": email}
            if category:
                lead_data["custom_fields"] = {"lead_category": category}

            await client.add_leads_to_campaign(
                campaign_id=campaign_id,
                lead_list=[lead_data],
                settings={
                    "ignore_duplicate_leads_in_other_campaign": True,
                    "ignore_global_block_list": True,
                    "ignore_unsubscribe_list": True,
                },
            )

            updated_lead = await client.get_lead_by_email(email)

            if existing_lead:
                updated_camps = (
                    updated_lead.get("lead_campaign_data", []) if updated_lead else []
                )
                results.append(
                    LeadResult(
                        email=email,
                        lead_id=existing_lead["id"],
                        status="existing",
                        campaigns_count=len(updated_camps),
                        message=f"Added to campaign. Now in {len(updated_camps)} campaign(s).",
                    )
                )
            else:
                if updated_lead:
                    camps = updated_lead.get("lead_campaign_data", [])
                    results.append(
                        LeadResult(
                            email=email,
                            lead_id=updated_lead["id"],
                            status="created",
                            campaigns_count=len(camps),
                            message=f"New lead created. Added to {len(camps)} campaign(s).",
                        )
                    )
                else:
                    results.append(
                        LeadResult(
                            email=email,
                            status="error",
                            message="Lead added but could not fetch details",
                        )
                    )
                    failed += 1
                    continue

            successful += 1

        except Exception as e:
            logger.error(f"Error processing lead {email}: {e}")
            results.append(LeadResult(email=email, status="error", message=str(e)))
            failed += 1

    await client.close()

    return ClassifyResponse(
        campaign_id=campaign_id,
        campaign_name=campaign_name,
        total_processed=len(results),
        successful=successful,
        failed=failed,
        results=results,
    )


@app.get("/api/lead-lookup", response_model=LeadLookupResponse)
async def lead_lookup(email: str, request: Request):
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Invalid email address")

    api_key = resolve_api_key(request)
    client = get_client(api_key)

    try:
        lead = await client.get_lead_by_email(email)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Smartlead API error: {e}")
    finally:
        await client.close()

    if not lead:
        return LeadLookupResponse(email=email, found=False)

    campaigns_data = lead.get("lead_campaign_data", [])
    campaigns = [
        LeadCampaignInfo(
            campaign_id=c["campaign_id"],
            campaign_name=c["campaign_name"],
            campaign_lead_map_id=c["campaign_lead_map_id"],
            lead_category_id=c.get("lead_category_id"),
        )
        for c in campaigns_data
    ]

    return LeadLookupResponse(
        email=email,
        lead_id=lead.get("id"),
        found=True,
        first_name=lead.get("first_name"),
        last_name=lead.get("last_name"),
        company_name=lead.get("company_name"),
        campaigns_count=len(campaigns),
        campaigns=campaigns,
    )


# ====================================================================
# Frontend — served from Python (no Node.js needed)
# ====================================================================
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Smartlead Lead Manager</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px;color:#111;background:#f5f5f4;min-height:100vh}
.layout{display:flex;min-height:100vh}
/* Sidebar */
.sidebar{width:220px;background:#fff;border-right:1px solid #e5e7eb;display:flex;flex-direction:column;position:fixed;top:0;left:0;height:100vh;z-index:10}
.sidebar-logo{display:flex;align-items:center;gap:10px;padding:18px 16px;border-bottom:1px solid #e5e7eb}
.logo-icon{width:34px;height:34px;border-radius:8px;background:#4f46e5;display:flex;align-items:center;justify-content:center;color:#fff;font-size:18px;flex-shrink:0}
.logo-title{font-size:13px;font-weight:600;line-height:1.3}
.logo-sub{font-size:11px;color:#6b7280}
nav{flex:1;padding:10px 8px}
.nav-btn{display:flex;align-items:center;gap:10px;width:100%;padding:9px 10px;border:none;background:none;border-radius:8px;cursor:pointer;font-size:13px;color:#6b7280;text-align:left;transition:all .15s;margin-bottom:2px}
.nav-btn:hover{background:#f3f4f6;color:#111}
.nav-btn.active{background:#eef2ff;color:#4f46e5;font-weight:500}
.nav-btn svg{flex-shrink:0}
.sidebar-footer{padding:14px 16px;border-top:1px solid #e5e7eb}
.api-badge{display:flex;align-items:center;gap:7px;font-size:11px;color:#6b7280}
.dot{width:7px;height:7px;border-radius:50%;background:#d1d5db;flex-shrink:0;transition:background .3s}
.dot.ok{background:#22c55e}
.dot.err{background:#ef4444}
/* Main */
.main{margin-left:220px;flex:1;padding:28px 32px;max-width:calc(100vw - 220px)}
/* Page header */
.page-header{margin-bottom:24px}
.page-header h1{font-size:20px;font-weight:600;margin-bottom:4px}
.page-header p{font-size:13px;color:#6b7280}
/* Cards */
.card{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:20px;margin-bottom:16px}
.card-title{font-size:13px;font-weight:600;margin-bottom:4px;color:#111}
.card-desc{font-size:12px;color:#6b7280;margin-bottom:16px}
/* Grid */
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
/* Forms */
label{font-size:12px;font-weight:500;color:#374151;display:block;margin-bottom:5px}
input[type=text],input[type=email],input[type=password],select{width:100%;padding:8px 11px;border:1px solid #d1d5db;border-radius:8px;font-size:13px;color:#111;background:#fff;outline:none;font-family:inherit;transition:border .15s}
input:focus,select:focus{border-color:#4f46e5;box-shadow:0 0 0 3px rgba(79,70,229,.1)}
.field{margin-bottom:14px}
/* Buttons */
.btn{display:inline-flex;align-items:center;gap:7px;padding:8px 16px;border-radius:8px;font-size:13px;font-weight:500;cursor:pointer;border:1px solid #d1d5db;background:#fff;color:#111;transition:all .15s;font-family:inherit;white-space:nowrap}
.btn:hover{background:#f9fafb}
.btn:disabled{opacity:.5;cursor:not-allowed}
.btn-primary{background:#4f46e5;color:#fff;border-color:#4f46e5}
.btn-primary:hover:not(:disabled){background:#4338ca;border-color:#4338ca}
.btn-full{width:100%;justify-content:center}
.btn-row{display:flex;gap:8px;align-items:center}
/* Drop zone */
.dropzone{border:2px dashed #d1d5db;border-radius:10px;padding:32px 16px;text-align:center;cursor:pointer;transition:all .15s;margin-bottom:14px}
.dropzone:hover,.dropzone.drag{border-color:#4f46e5;background:#eef2ff22}
.dropzone svg{margin:0 auto 10px;display:block;color:#9ca3af}
.dz-label{font-size:13px;font-weight:500;margin-bottom:3px}
.dz-sub{font-size:11px;color:#9ca3af}
.file-chip{display:flex;align-items:center;gap:10px;background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;padding:10px 12px;font-size:13px;margin-bottom:14px}
.file-chip svg{color:#22c55e;flex-shrink:0}
.fc-name{font-weight:500;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fc-size{font-size:11px;color:#9ca3af;white-space:nowrap}
.fc-del{background:none;border:none;cursor:pointer;color:#9ca3af;padding:0;display:flex;align-items:center;transition:color .15s}
.fc-del:hover{color:#111}
/* Alert */
.alert{display:flex;align-items:flex-start;gap:9px;padding:11px 14px;border-radius:8px;font-size:13px;margin-bottom:14px}
.alert.err{background:#fef2f2;color:#991b1b;border:1px solid #fecaca}
.alert.ok{background:#f0fdf4;color:#166534;border:1px solid #bbf7d0}
.alert.warn{background:#fffbeb;color:#92400e;border:1px solid #fde68a}
.alert svg{flex-shrink:0;margin-top:1px}
/* Summary metrics */
.metrics{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:14px}
.metric{padding:12px;background:#f9fafb;border-radius:8px;text-align:center}
.mv{font-size:22px;font-weight:600;line-height:1.2}
.ml{font-size:11px;color:#6b7280;margin-top:2px}
.mv.green{color:#16a34a}
.mv.red{color:#dc2626}
.mv.blue{color:#4f46e5}
/* Progress */
.prog-bar{height:5px;background:#e5e7eb;border-radius:3px;overflow:hidden;margin:6px 0}
.prog-fill{height:100%;background:#4f46e5;border-radius:3px;transition:width .3s}
/* Table */
.tbl-wrap{overflow:auto;border:1px solid #e5e7eb;border-radius:10px;max-height:280px}
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;padding:9px 12px;color:#6b7280;font-weight:500;border-bottom:1px solid #e5e7eb;white-space:nowrap;position:sticky;top:0;background:#fff;z-index:1}
td{padding:8px 12px;border-bottom:1px solid #f3f4f6;vertical-align:middle}
tr:last-child td{border-bottom:none}
.badge{display:inline-flex;align-items:center;gap:3px;padding:2px 9px;border-radius:20px;font-size:11px;font-weight:500}
.badge.created{background:#dcfce7;color:#15803d}
.badge.existing{background:#dbeafe;color:#1d4ed8}
.badge.error{background:#fee2e2;color:#b91c1c}
/* Lead lookup result */
.lookup-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:14px}
.info-tile{display:flex;align-items:center;gap:10px;padding:11px 13px;background:#f9fafb;border-radius:8px;border:1px solid #e5e7eb}
.info-tile svg{color:#9ca3af;flex-shrink:0}
.it-lbl{font-size:11px;color:#6b7280}
.it-val{font-size:13px;font-weight:500;color:#111}
.it-val.accent{color:#4f46e5}
.found-banner{display:flex;align-items:center;gap:8px;font-size:14px;font-weight:600;margin-bottom:16px}
.found-banner.yes{color:#15803d}
.found-banner.no{color:#b91c1c}
.camp-list{border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;margin-top:12px}
.camp-row{display:flex;align-items:center;gap:8px;padding:9px 12px;border-bottom:1px solid #f3f4f6;font-size:12px}
.camp-row:last-child{border-bottom:none}
.cr-num{color:#9ca3af;width:18px;text-align:right;flex-shrink:0}
.cr-name{flex:1;font-weight:500}
.cr-id{font-size:11px;color:#9ca3af;font-family:monospace}
/* Settings */
.tip{font-size:11px;color:#6b7280;margin-top:5px;line-height:1.6}
.tip a{color:#4f46e5}
pre.example{font-size:11px;background:#f3f4f6;border-radius:6px;padding:10px;font-family:monospace;line-height:1.7;overflow-x:auto}
/* Empty state */
.empty{text-align:center;padding:36px 16px;color:#9ca3af}
.empty svg{margin:0 auto 10px;display:block;opacity:.4}
.empty p{font-size:13px}
/* Spinner */
.spin{display:inline-block;width:14px;height:14px;border:2px solid rgba(255,255,255,.35);border-top-color:#fff;border-radius:50%;animation:_spin .65s linear infinite;vertical-align:-2px}
.spin.dark{border:2px solid #d1d5db;border-top-color:#4f46e5}
@keyframes _spin{to{transform:rotate(360deg)}}
/* Page visibility */
.page{display:none}.page.active{display:block}
/* Responsive */
@media(max-width:700px){.grid2,.lookup-grid,.metrics{grid-template-columns:1fr}.main{padding:16px}.sidebar{width:100%;height:auto;position:relative}.layout{flex-direction:column}.main{margin-left:0;max-width:100%}}
</style>
</head>
<body>
<div class="layout">
  <!-- Sidebar -->
  <aside class="sidebar">
    <div class="sidebar-logo">
      <div class="logo-icon">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg>
      </div>
      <div>
        <div class="logo-title">Smartlead</div>
        <div class="logo-sub">Lead Manager</div>
      </div>
    </div>
    <nav>
      <button class="nav-btn active" onclick="showPage('upload')" id="nav-upload">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
        Upload &amp; Classify
      </button>
      <button class="nav-btn" onclick="showPage('lookup')" id="nav-lookup">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
        Lead Lookup
      </button>
      <button class="nav-btn" onclick="showPage('settings')" id="nav-settings">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14M4.93 4.93a10 10 0 0 0 0 14.14"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2"/></svg>
        Settings
      </button>
    </nav>
    <div class="sidebar-footer">
      <div class="api-badge">
        <div class="dot" id="dot"></div>
        <span id="api-status-text">Checking...</span>
      </div>
    </div>
  </aside>

  <main class="main">

    <!-- ==================== UPLOAD PAGE ==================== -->
    <div class="page active" id="page-upload">
      <div class="page-header">
        <h1>Upload &amp; Classify Leads</h1>
        <p>Upload a CSV or Excel file with emails, then assign them to a campaign.</p>
      </div>
      <div id="upload-alert"></div>
      <div class="grid2">
        <!-- Left: Upload + Campaign -->
        <div>
          <div class="card">
            <div class="card-title">1. Upload file</div>
            <div class="card-desc">Must have an <code>email</code> column. Optional: <code>lead_category</code></div>
            <div class="dropzone" id="dropzone" onclick="document.getElementById('file-input').click()"
              ondragover="event.preventDefault();this.classList.add('drag')"
              ondragleave="this.classList.remove('drag')"
              ondrop="onDrop(event)">
              <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="M2 10h20M7 4v6M12 4v6M17 4v6"/></svg>
              <div class="dz-label">Drag &amp; drop or click to upload</div>
              <div class="dz-sub">Supports .CSV and .XLSX</div>
            </div>
            <input type="file" id="file-input" accept=".csv,.xlsx,.xls" style="display:none" onchange="onFileChange(this)">
            <div id="file-chip" style="display:none"></div>
          </div>
          <div class="card">
            <div class="card-title">2. Select campaign</div>
            <div class="card-desc">Choose which campaign to assign the leads to.</div>
            <div class="field">
              <select id="campaign-select" onchange="selCampaign=this.value;checkBtn()">
                <option value="">Loading campaigns...</option>
              </select>
            </div>
            <button class="btn btn-primary btn-full" id="process-btn" onclick="processLeads()" disabled>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polygon points="5 3 19 12 5 21 5 3"/></svg>
              Start Operation
            </button>
          </div>
        </div>
        <!-- Right: Summary -->
        <div>
          <div class="card" style="height:100%">
            <div class="card-title">Operation Summary</div>
            <div class="card-desc">Results appear here after processing.</div>
            <div id="summary">
              <div class="empty">
                <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
                <p>No operation yet</p>
              </div>
            </div>
          </div>
        </div>
      </div>
      <!-- Results table -->
      <div id="results-wrap" style="display:none">
        <div class="card">
          <div class="card-title" id="results-title">Detailed Results</div>
          <div class="tbl-wrap" id="results-tbl"></div>
        </div>
      </div>
    </div>

    <!-- ==================== LOOKUP PAGE ==================== -->
    <div class="page" id="page-lookup">
      <div class="page-header">
        <h1>Lead Lookup</h1>
        <p>Search by email to see which campaigns a lead belongs to.</p>
      </div>
      <div id="lookup-alert"></div>
      <div class="card">
        <div class="card-title">Search by email</div>
        <div class="card-desc">Look up a lead's Smartlead ID and all their campaign memberships.</div>
        <div class="btn-row">
          <input type="email" id="lookup-input" placeholder="lead@example.com" onkeydown="if(event.key==='Enter')doLookup()" style="flex:1">
          <button class="btn btn-primary" id="lookup-btn" onclick="doLookup()">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
            Search
          </button>
        </div>
      </div>
      <div id="lookup-result"></div>
    </div>

    <!-- ==================== SETTINGS PAGE ==================== -->
    <div class="page" id="page-settings">
      <div class="page-header">
        <h1>Settings</h1>
        <p>Configure your Smartlead API connection.</p>
      </div>
      <div class="card">
        <div class="card-title" style="margin-bottom:16px;padding-bottom:12px;border-bottom:1px solid #f3f4f6">API Key</div>
        <div class="field">
          <label for="key-input">Smartlead API Key</label>
          <input type="password" id="key-input" placeholder="Paste your API key here..." autocomplete="off">
          <div class="tip">
            Get your key at <a href="https://app.smartlead.ai/app/settings/profile" target="_blank">Smartlead → Settings → Profile</a>.<br>
            The key is stored in your browser session only. Alternatively, set <code>SMARTLEAD_API_KEY</code> in the <code>.env</code> file to skip this step.
          </div>
        </div>
        <button class="btn btn-primary" id="save-btn" onclick="saveKey()">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>
          Save &amp; Test
        </button>
        <div id="key-result" style="margin-top:12px"></div>
      </div>
      <div class="card">
        <div class="card-title" style="margin-bottom:12px">CSV format reference</div>
        <p style="font-size:12px;color:#6b7280;margin-bottom:10px">Your upload file must have an <code>email</code> column. <code>lead_category</code> is optional.</p>
        <pre class="example">email,lead_category
john@example.com,Interested
jane@example.com,Hot Lead
bob@example.com,</pre>
      </div>
    </div>

  </main>
</div>

<script>
const API = '';   // same origin
let apiKey = '';
let campaigns = [];
let selFile = null;
let selCampaign = '';

// ---- Navigation ----
function showPage(name) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('page-' + name).classList.add('active');
  document.getElementById('nav-' + name).classList.add('active');
}

// ---- API key handling ----
function headers() {
  const h = {'X-API-Key': apiKey};
  return h;
}

function setStatus(state, text) {
  const dot = document.getElementById('dot');
  const txt = document.getElementById('api-status-text');
  dot.className = 'dot' + (state === 'ok' ? ' ok' : state === 'err' ? ' err' : '');
  txt.textContent = text;
}

// ---- Alert helper ----
function alert$(id, type, msg) {
  const el = document.getElementById(id);
  if (!msg) { el.innerHTML = ''; return; }
  const icons = {
    err: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>',
    ok:  '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg>',
    warn:'<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
  };
  el.innerHTML = `<div class="alert ${type}">${icons[type]||''}<span>${msg}</span></div>`;
}

// ---- Load campaigns ----
async function loadCampaigns() {
  const sel = document.getElementById('campaign-select');
  if (!apiKey) { sel.innerHTML = '<option value="">Set API key in Settings first</option>'; setStatus('', 'No API key'); return; }
  sel.innerHTML = '<option value="">Loading...</option>';
  try {
    const r = await fetch(API + '/api/campaigns', {headers: headers()});
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    const data = await r.json();
    campaigns = data.campaigns || [];
    sel.innerHTML = '<option value="">Choose a campaign...</option>' +
      campaigns.map(c => `<option value="${c.id}">${c.name} (ID: ${c.id})</option>`).join('');
    setStatus('ok', 'Connected');
  } catch(e) {
    sel.innerHTML = '<option value="">Failed to load campaigns</option>';
    setStatus('err', 'Connection error');
  }
}

// ---- File handling ----
function onDrop(e) {
  e.preventDefault();
  document.getElementById('dropzone').classList.remove('drag');
  if (e.dataTransfer.files[0]) setFile(e.dataTransfer.files[0]);
}
function onFileChange(input) { if (input.files[0]) setFile(input.files[0]); }
function setFile(f) {
  const ok = /\\.(csv|xlsx|xls)$/i.test(f.name);
  if (!ok) { alert$('upload-alert','err','Please upload a .csv or .xlsx file'); return; }
  selFile = f;
  document.getElementById('dropzone').style.display = 'none';
  const chip = document.getElementById('file-chip');
  chip.style.display = 'flex';
  chip.className = 'file-chip';
  chip.innerHTML = `
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#22c55e" stroke-width="2"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="M2 10h20M7 4v6M12 4v6M17 4v6"/></svg>
    <span class="fc-name">${f.name}</span>
    <span class="fc-size">${(f.size/1024).toFixed(1)} KB</span>
    <button class="fc-del" onclick="clearFile()">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
    </button>`;
  alert$('upload-alert','','');
  checkBtn();
}
function clearFile() {
  selFile = null;
  document.getElementById('dropzone').style.display = '';
  document.getElementById('file-chip').style.display = 'none';
  document.getElementById('file-input').value = '';
  checkBtn();
}
function checkBtn() {
  document.getElementById('process-btn').disabled = !(selFile && selCampaign);
}

// ---- Process leads ----
async function processLeads() {
  if (!selFile || !selCampaign) { alert$('upload-alert','err','Please select a file and a campaign'); return; }
  if (!apiKey) { alert$('upload-alert','err','Set your API key in Settings first'); return; }

  const btn = document.getElementById('process-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> Processing...';
  alert$('upload-alert','','');
  document.getElementById('results-wrap').style.display = 'none';

  const formData = new FormData();
  formData.append('file', selFile);
  formData.append('campaign_id', selCampaign);

  try {
    setSummaryProcessing('Sending to server...');
    const r = await fetch(API + '/api/upload-and-classify', {
      method: 'POST',
      headers: headers(),
      body: formData
    });
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      throw new Error(e.detail || `HTTP ${r.status}`);
    }
    const data = await r.json();
    renderSummary(data);
    renderTable(data.results);
  } catch(e) {
    alert$('upload-alert','err', e.message || String(e));
    document.getElementById('summary').innerHTML = '<div class="empty"><p>Processing failed</p></div>';
  }

  btn.disabled = false;
  btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polygon points="5 3 19 12 5 21 5 3"/></svg> Start Operation';
}

function setSummaryProcessing(msg) {
  document.getElementById('summary').innerHTML = `
    <div style="text-align:center;padding:24px 0">
      <div class="spin dark" style="width:20px;height:20px;border-width:3px;margin:0 auto 10px"></div>
      <p style="font-size:13px;color:#6b7280">${msg}</p>
    </div>`;
}

function renderSummary(d) {
  const pct = d.total_processed > 0 ? Math.round(d.successful / d.total_processed * 100) : 0;
  document.getElementById('summary').innerHTML = `
    <div class="metrics">
      <div class="metric"><div class="mv">${d.total_processed}</div><div class="ml">Total</div></div>
      <div class="metric"><div class="mv green">${d.successful}</div><div class="ml">Successful</div></div>
      <div class="metric"><div class="mv red">${d.failed}</div><div class="ml">Failed</div></div>
      <div class="metric"><div class="mv blue">${d.campaign_id}</div><div class="ml">Campaign ID</div></div>
    </div>
    <div style="font-size:12px;color:#6b7280;margin-bottom:10px">Campaign: <strong style="color:#111">${d.campaign_name}</strong></div>
    <div style="display:flex;justify-content:space-between;font-size:11px;font-weight:500;margin-bottom:4px"><span>Success rate</span><span>${pct}%</span></div>
    <div class="prog-bar"><div class="prog-fill" style="width:${pct}%"></div></div>`;
}

function renderTable(results) {
  if (!results.length) return;
  document.getElementById('results-title').textContent = `Detailed Results (${results.length} leads)`;
  const rows = results.map((r,i) => {
    const badge =
      r.status === 'created'  ? '<span class="badge created">Created</span>' :
      r.status === 'existing' ? '<span class="badge existing">Existing</span>' :
                                '<span class="badge error">Error</span>';
    const msg = r.message.length > 60 ? r.message.slice(0,60)+'…' : r.message;
    return `<tr>
      <td style="color:#9ca3af">${i+1}</td>
      <td style="font-weight:500;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${r.email}</td>
      <td>${r.lead_id ? `<code style="font-size:11px;background:#f3f4f6;padding:2px 6px;border-radius:4px">${r.lead_id}</code>` : '<span style="color:#9ca3af">—</span>'}</td>
      <td>${badge}</td>
      <td style="text-align:center"><span style="display:inline-flex;width:22px;height:22px;align-items:center;justify-content:center;background:#f3f4f6;border-radius:50%;font-size:11px;font-weight:600">${r.campaigns_count}</span></td>
      <td style="color:#6b7280">${msg}</td>
    </tr>`;
  }).join('');
  document.getElementById('results-tbl').innerHTML = `
    <table><thead><tr><th>#</th><th>Email</th><th>Lead ID</th><th>Status</th><th style="text-align:center">Campaigns</th><th>Message</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
  document.getElementById('results-wrap').style.display = '';
}

// ---- Lead Lookup ----
async function doLookup() {
  const email = document.getElementById('lookup-input').value.trim();
  if (!email || !email.includes('@')) { alert$('lookup-alert','err','Enter a valid email address'); return; }
  if (!apiKey) { alert$('lookup-alert','err','Set your API key in Settings first'); return; }

  const btn = document.getElementById('lookup-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> Searching...';
  alert$('lookup-alert','','');
  document.getElementById('lookup-result').innerHTML = '';

  try {
    const r = await fetch(`${API}/api/lead-lookup?email=${encodeURIComponent(email)}`, {headers: headers()});
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      throw new Error(e.detail || `HTTP ${r.status}`);
    }
    const data = await r.json();
    renderLookup(data);
  } catch(e) {
    alert$('lookup-alert','err', e.message || String(e));
  }

  btn.disabled = false;
  btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg> Search';
}

function renderLookup(d) {
  const el = document.getElementById('lookup-result');
  if (!d.found) {
    el.innerHTML = `<div class="card">
      <div class="found-banner no">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>
        Lead not found
      </div>
      <p style="font-size:13px;color:#6b7280">No lead found with email <strong>${d.email}</strong>.</p>
    </div>`;
    return;
  }
  const name = [d.first_name, d.last_name].filter(Boolean).join(' ') || 'N/A';
  const campRows = (d.campaigns || []).map((c,i) =>
    `<div class="camp-row">
      <span class="cr-num">${i+1}</span>
      <span class="cr-name">${c.campaign_name || '—'}</span>
      <span class="cr-id">ID: ${c.campaign_id}</span>
      ${c.lead_category_id ? `<span class="badge existing" style="font-size:10px">Cat: ${c.lead_category_id}</span>` : ''}
    </div>`).join('');
  el.innerHTML = `<div class="card">
    <div class="found-banner yes">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg>
      Lead found
    </div>
    <div class="lookup-grid">
      <div class="info-tile">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg>
        <div><div class="it-lbl">Email</div><div class="it-val" style="font-size:12px">${d.email}</div></div>
      </div>
      <div class="info-tile">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
        <div><div class="it-lbl">Name</div><div class="it-val">${name}</div></div>
      </div>
      <div class="info-tile">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/></svg>
        <div><div class="it-lbl">Company</div><div class="it-val">${d.company_name || 'N/A'}</div></div>
      </div>
      <div class="info-tile" style="background:#eef2ff;border-color:#c7d2fe">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#4f46e5" stroke-width="2"><rect x="2" y="7" width="20" height="14" rx="2"/><path d="M16 7V5a2 2 0 0 0-4 0v2M12 12v4"/><line x1="12" y1="12" x2="12" y2="16"/></svg>
        <div><div class="it-lbl">Smartlead ID</div><div class="it-val accent">${d.lead_id}</div></div>
      </div>
    </div>
    <p style="font-size:13px;margin-bottom:${d.campaigns.length?'0':'0'}">
      This lead is in <strong>${d.campaigns_count}</strong> campaign${d.campaigns_count !== 1 ? 's' : ''}.
    </p>
    ${campRows ? `<div class="camp-list">${campRows}</div>` : ''}
  </div>`;
}

// ---- Settings: Save & Test ----
async function saveKey() {
  const val = document.getElementById('key-input').value.trim();
  const res = document.getElementById('key-result');
  if (!val) { res.innerHTML = '<div class="alert err"><span>Please enter an API key.</span></div>'; return; }

  const btn = document.getElementById('save-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> Testing...';
  res.innerHTML = '';
  setStatus('', 'Testing...');

  try {
    const r = await fetch(API + '/api/health', {headers: {'X-API-Key': val}});
    const data = await r.json();
    if (data.status === 'ok' && data.api_key_configured) {
      apiKey = val;
      document.getElementById('key-input').value = '';
      document.getElementById('key-input').placeholder = '••••••••••••••••••••••';
      res.innerHTML = `<div class="alert ok"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg><span>Connected! Found ${data.campaigns_count} campaign(s).</span></div>`;
      setStatus('ok', 'Connected');
      loadCampaigns();
    } else {
      throw new Error(data.detail || 'Invalid API key');
    }
  } catch(e) {
    res.innerHTML = `<div class="alert err"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg><span>${e.message || 'Connection failed'}</span></div>`;
    setStatus('err', 'Error');
  }
  btn.disabled = false;
  btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg> Save &amp; Test';
}

// ---- Init ----
window.addEventListener('load', () => {
  // Auto-load if API key is already in .env (backend returns it in health check)
  fetch(API + '/api/health').then(r => r.json()).then(data => {
    if (data.api_key_configured) {
      // Key is in .env — use server-side key (send empty header, backend falls back to env)
      setStatus('ok', 'Key from .env');
      apiKey = '__env__';  // sentinel: triggers header but backend also reads env
      loadCampaigns();
    } else {
      setStatus('', 'No API key');
      document.getElementById('campaign-select').innerHTML = '<option value="">Set API key in Settings first</option>';
    }
  }).catch(() => {
    setStatus('err', 'Server error');
  });
});
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    return HTMLResponse(content=HTML)


# ====================================================================
# Entry point
# ====================================================================
if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", ""))
    print(f"""
╔══════════════════════════════════════════════╗
║       Smartlead Lead Manager                 ║
╠══════════════════════════════════════════════╣
║  Open in browser:  http://localhost:{port}      ║
║  API docs:         http://localhost:{port}/docs  ║
╚══════════════════════════════════════════════╝

Tip: add your API key to .env to skip the Settings step.
""")
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=True)
