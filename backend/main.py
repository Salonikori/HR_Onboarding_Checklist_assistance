from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
from collections import Counter
import csv, math, re, secrets, sqlite3, os

from fastapi import FastAPI, HTTPException, Header, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field
import requests
import numpy as np
try:
    from transformers import AutoTokenizer, AutoModel
    import torch
except Exception:
    AutoTokenizer = AutoModel = torch = None

from backend import email_service
from backend import scheduler as reminder_scheduler

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_FILE = ROOT / "onboarding.db"
TASK_FILE = DATA / "employee_onboarding_tasks.csv"
GUIDE_FILE = DATA / "onboarding_guides_60_roles_detailed.csv"
EMP_CREDS = DATA / "employee_login_credentials.csv"
HR_CREDS = DATA / "hr_login_credentials.csv"
HIGH_THRESHOLD = 0.80
MEDIUM_THRESHOLD = 0.60
NO_MATCH_THRESHOLD = 0.15
SEMANTIC_MODEL_NAME = os.getenv('SEMANTIC_MODEL_NAME', 'sentence-transformers/all-MiniLM-L6-v2')
GROQ_MODEL = os.getenv('GROQ_MODEL', 'llama-3.1-8b-instant')
GROQ_API_URL = 'https://api.groq.com/openai/v1/chat/completions'
SEMANTIC_TOKENIZER = None
SEMANTIC_MODEL = None
SEMANTIC_DEVICE = 'cpu'

app = FastAPI(title="OnboardAI Unified HR + Employee", version="4.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"], allow_credentials=False)

SESSIONS = {}
GUIDES = []
GUIDE_VECTORS = {}
EMPLOYEE_GUIDE = {}

class LoginBody(BaseModel):
    login_id: str
    password: str

class QueryBody(BaseModel):
    query: str = Field(min_length=2, max_length=500)

class StatusBody(BaseModel):
    status: str

class ReviewBody(BaseModel):
    action: str = Field(pattern=r"^(approve|override)$")
    note: str = Field(min_length=1, max_length=2000)

class EmailBody(BaseModel):
    email: str = Field(min_length=5, max_length=200)

class CreateEmployeeBody(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: str = Field(min_length=5, max_length=200)
    role: str = Field(min_length=2, max_length=120)
    department: str = Field(min_length=2, max_length=120)
    joining_date: str = Field(min_length=8, max_length=20)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# -------- DB --------
def db_conn():
    c=sqlite3.connect(DB_FILE)
    c.row_factory=sqlite3.Row
    return c

def init_db():
    c=db_conn()
    c.executescript('''
    CREATE TABLE IF NOT EXISTS employees (
      employee_id TEXT PRIMARY KEY, role TEXT, department TEXT, seniority TEXT,
      job_family TEXT, joining_date TEXT, guide_id TEXT, email TEXT
    );
    CREATE TABLE IF NOT EXISTS guides (
      guide_id TEXT, task_id TEXT PRIMARY KEY, role TEXT, department TEXT,
      phase TEXT, category TEXT, task_name TEXT, task_description TEXT,
      action_required_by TEXT, task_owner TEXT, required TEXT, expected_evidence TEXT,
      approved_next_step TEXT, next_step_owner TEXT, reminder_template TEXT,
      escalation_rule TEXT, source_reference TEXT, ml_training_text TEXT, guide_text TEXT
    );
    CREATE TABLE IF NOT EXISTS tasks (
      employee_id TEXT, task_id TEXT, guide_id TEXT, task_order INTEGER, phase TEXT,
      category TEXT, task_name TEXT, task_description TEXT, task_owner TEXT,
      action_required_by TEXT, required TEXT, status TEXT, joining_date TEXT,
      due_date TEXT, is_overdue INTEGER, evidence TEXT, issue_text TEXT,
      approved_next_step TEXT, next_step_owner TEXT, employee_action TEXT,
      reminder_template TEXT, escalation_rule TEXT, reviewer_action TEXT,
      review_timestamp TEXT, PRIMARY KEY(employee_id, task_id)
    );
    CREATE TABLE IF NOT EXISTS onboarding_queries (
      id INTEGER PRIMARY KEY AUTOINCREMENT, employee_id TEXT, query TEXT,
      matched_guide_id TEXT, matched_task_id TEXT, matched_task_name TEXT,
      category TEXT, similarity_score REAL, confidence_band TEXT,
      requires_human_review INTEGER, status TEXT, is_overdue INTEGER,
      approved_next_step TEXT, next_step_owner TEXT, source_reference TEXT,
      created_at TEXT, reviewer_action TEXT, reviewer_note TEXT, reviewed_at TEXT
    );
    CREATE TABLE IF NOT EXISTS audit_log (
      id INTEGER PRIMARY KEY AUTOINCREMENT, actor_role TEXT, actor_id TEXT,
      event_type TEXT, employee_id TEXT, task_id TEXT, query_id INTEGER,
      details TEXT, created_at TEXT
    );
    ''')
    c.commit()
    existing_qcols = {row[1] for row in c.execute('PRAGMA table_info(onboarding_queries)').fetchall()}
    if 'grounded_explanation' not in existing_qcols:
        c.execute('ALTER TABLE onboarding_queries ADD COLUMN grounded_explanation TEXT')
        c.commit()
    # Migration guard: older databases created before email notifications
    # were added won't have this column yet.
    existing_cols = {row[1] for row in c.execute('PRAGMA table_info(employees)').fetchall()}
    if 'email' not in existing_cols:
        c.execute('ALTER TABLE employees ADD COLUMN email TEXT')
        c.commit()
    # Seed only once.
    if c.execute('SELECT COUNT(*) FROM employees').fetchone()[0] == 0:
        with open(TASK_FILE, encoding='utf-8-sig', newline='') as f:
            rows=list(csv.DictReader(f))
        emails_by_eid={}
        if EMP_CREDS.exists():
            with open(EMP_CREDS, encoding='utf-8-sig', newline='') as f:
                for r in csv.DictReader(f):
                    emails_by_eid[r.get('employee_id','')]=r.get('email','')
        emp_seen={}
        for r in rows:
            eid=r['employee_id']; emp_seen[eid]=r
            c.execute('INSERT OR IGNORE INTO employees VALUES (?,?,?,?,?,?,?,?)',(
                eid,r.get('role',''),r.get('department',''),r.get('seniority',''),r.get('job_family',''),r.get('joining_date',''),r.get('guide_id',''),emails_by_eid.get(eid,'')))
            status='Pending' if r.get('status')=='Incomplete' else (r.get('status') or 'Pending')
            c.execute('INSERT OR REPLACE INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(
                eid,r.get('task_id',''),r.get('guide_id',''),int(float(r.get('task_order') or 0)),r.get('phase',''),r.get('category',''),r.get('task_name',''),r.get('task_description',''),r.get('task_owner',''),r.get('action_required_by',''),r.get('required',''),status,r.get('joining_date',''),r.get('due_date',''),1 if str(r.get('is_overdue','')).lower() in ('1','true','yes') else 0,r.get('evidence',''),r.get('issue_text',''),r.get('approved_next_step',''),r.get('next_step_owner',''),r.get('employee_action',''),r.get('reminder_template',''),r.get('escalation_rule',''),r.get('reviewer_action',''),r.get('review_timestamp','')))
        # guides
        with open(GUIDE_FILE, encoding='utf-8-sig', newline='') as f:
            for r in csv.DictReader(f):
                guide_text=' '.join([r.get('task_name',''),r.get('task_description',''),r.get('category',''),r.get('ml_training_text','')]).strip()
                c.execute('INSERT OR REPLACE INTO guides VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(
                  r.get('guide_id',''),r.get('task_id',''),r.get('role',''),r.get('department',''),r.get('phase',''),r.get('category',''),r.get('task_name',''),r.get('task_description',''),r.get('action_required_by',''),r.get('task_owner',''),r.get('required',''),r.get('expected_evidence',''),r.get('approved_next_step',''),r.get('next_step_owner',''),r.get('reminder_template',''),r.get('escalation_rule',''),r.get('source_reference',''),r.get('ml_training_text',''),guide_text))
        c.commit()
        # initial audit snapshot
        c.execute("INSERT INTO audit_log(actor_role,actor_id,event_type,details,created_at) VALUES(?,?,?,?,?)",('system','system','database_seeded',f'{len(rows)} task rows loaded',now()))
        c.commit()
    c.close()

def now(): return datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')

def audit(actor_role,actor_id,event_type,employee_id=None,task_id=None,query_id=None,details=''):
    c=db_conn(); c.execute('INSERT INTO audit_log(actor_role,actor_id,event_type,employee_id,task_id,query_id,details,created_at) VALUES(?,?,?,?,?,?,?,?)',(actor_role,actor_id,event_type,employee_id,task_id,query_id,details,now())); c.commit(); c.close()

# -------- email notifications --------
def get_employee_email(employee_id:str) -> str:
    """Look up an employee's email address for automated notifications."""
    c=db_conn(); row=c.execute('SELECT email FROM employees WHERE employee_id=?',(employee_id,)).fetchone(); c.close()
    return (row['email'] or '').strip() if row else ''

def set_employee_email(employee_id:str, email:str):
    c=db_conn(); c.execute('UPDATE employees SET email=? WHERE employee_id=?',(email,employee_id)); c.commit(); c.close()
    # Keep the CSV in sync so it stays the source of truth on a fresh reseed.
    if EMP_CREDS.exists():
        with open(EMP_CREDS, encoding='utf-8-sig', newline='') as f:
            reader=csv.DictReader(f); rows=list(reader)
            fieldnames=reader.fieldnames if reader.fieldnames and 'email' in reader.fieldnames else ['employee_id','login_id','password','email']
        for r in rows:
            if r.get('employee_id')==employee_id: r['email']=email
        with open(EMP_CREDS, 'w', encoding='utf-8', newline='') as f:
            w=csv.DictWriter(f, fieldnames=fieldnames); w.writeheader(); w.writerows(rows)

def get_login_id(employee_id:str) -> str:
    if EMP_CREDS.exists():
        with open(EMP_CREDS, encoding='utf-8-sig', newline='') as f:
            for r in csv.DictReader(f):
                if r.get('employee_id')==employee_id: return r.get('login_id','')
    return ''

def get_pending_tasks_by_employee() -> dict:
    """Group every not-yet-completed task by employee_id for the reminder scheduler."""
    c=db_conn()
    rows=c.execute("SELECT employee_id,task_name,due_date,status,is_overdue,action_required_by FROM tasks WHERE status!='Completed'").fetchall()
    c.close()
    grouped={}
    for r in rows:
        grouped.setdefault(r['employee_id'],[]).append(dict(r))
    return grouped

# -------- semantic matching: transformer sentence embeddings --------
def _mean_pool(last_hidden_state, attention_mask):
    mask = attention_mask.unsqueeze(-1).expand(last_hidden_state.size()).float()
    summed = torch.sum(last_hidden_state * mask, dim=1)
    counts = torch.clamp(mask.sum(dim=1), min=1e-9)
    return summed / counts

def _load_semantic_model():
    global SEMANTIC_TOKENIZER, SEMANTIC_MODEL, SEMANTIC_DEVICE
    if SEMANTIC_MODEL is not None:
        return
    if AutoTokenizer is None or AutoModel is None or torch is None:
        raise RuntimeError(
            "Semantic model dependencies are missing. Install the packages from requirements.txt."
        )
    SEMANTIC_TOKENIZER = AutoTokenizer.from_pretrained(SEMANTIC_MODEL_NAME)
    SEMANTIC_MODEL = AutoModel.from_pretrained(SEMANTIC_MODEL_NAME)
    SEMANTIC_MODEL.eval()
    SEMANTIC_DEVICE = 'cpu'
    SEMANTIC_MODEL.to(SEMANTIC_DEVICE)

def embed_texts(texts):
    _load_semantic_model()
    encoded = SEMANTIC_TOKENIZER(
        list(texts), padding=True, truncation=True, max_length=256, return_tensors='pt'
    )
    with torch.no_grad():
        output = SEMANTIC_MODEL(**encoded)
        pooled = _mean_pool(output.last_hidden_state, encoded['attention_mask'])
    vec = pooled.cpu().numpy().astype(np.float32)
    norms = np.linalg.norm(vec, axis=1, keepdims=True)
    return vec / np.maximum(norms, 1e-12)

def prepare_index():
    global GUIDES, GUIDE_VECTORS
    c=db_conn()
    rows=[dict(r) for r in c.execute('SELECT * FROM guides').fetchall()]
    c.close()
    GUIDES=rows
    # Build a clean semantic representation from approved guide fields only.
    texts=[
        ' '.join([
            r.get('task_name',''),
            r.get('task_description',''),
            r.get('category',''),
            r.get('ml_training_text','')
        ]).strip()
        for r in rows
    ]
    vectors=embed_texts(texts) if texts else np.empty((0,384), dtype=np.float32)
    GUIDE_VECTORS={r['task_id']:v for r,v in zip(rows,vectors)}

def match_query(employee_id, query):
    c=db_conn()
    emp=c.execute('SELECT * FROM employees WHERE employee_id=?',(employee_id,)).fetchone()
    if not emp:
        c.close()
        raise HTTPException(404,'Employee not found')
    candidates=[r for r in GUIDES if r['guide_id']==emp['guide_id']]
    c.close()
    if not candidates:
        raise HTTPException(500,'No approved onboarding guide tasks available for this employee')
    query_vec=embed_texts([query])[0]
    scored=[]
    for r in candidates:
        v=GUIDE_VECTORS.get(r['task_id'])
        if v is None:
            continue
        score=float(np.dot(query_vec, v))
        scored.append((score,r))
    if not scored:
        raise HTTPException(500,'Unable to calculate semantic similarity')
    score,row=max(scored,key=lambda x:x[0])
    score=round(float(score),4)
    if score>=HIGH_THRESHOLD: band='HIGH'; review=0
    elif score>=MEDIUM_THRESHOLD: band='MEDIUM'; review=1
    elif score>=NO_MATCH_THRESHOLD: band='LOW'; review=1
    else: band='NO_MATCH'; review=1
    return row,score,band,review

# -------- auth --------
def session(authorization:Optional[str]=Header(default=None)):
    if not authorization or not authorization.startswith('Bearer '): raise HTTPException(401,'Authentication required')
    s=SESSIONS.get(authorization.split(' ',1)[1])
    if not s: raise HTTPException(401,'Invalid session')
    return s

def emp_session(s=Depends(session)):
    if s.get('role')!='employee': raise HTTPException(403,'Employee access required')
    return s

def hr_session(s=Depends(session)):
    if s.get('role')!='hr': raise HTTPException(403,'HR access required')
    return s

@app.on_event('startup')
def on_start():
    init_db(); prepare_index()
    # Automated reminder emails: runs in the background on a schedule, no HR input required.
    reminder_scheduler.start_scheduler(get_pending_tasks_by_employee=get_pending_tasks_by_employee, get_employee_email=get_employee_email)

@app.on_event('shutdown')
def on_stop():
    reminder_scheduler.stop_scheduler()

@app.get('/health')
@app.get('/api/health')
def health(): return {'status':'ok','service':'onboardai'}

@app.get('/',include_in_schema=False)
def root(): return FileResponse(ROOT/'frontend'/'index.html')
@app.get('/hr',include_in_schema=False)
def hr(): return FileResponse(ROOT/'frontend'/'hr-dashboard.html')

@app.post('/api/login')
def login(body:LoginBody):
    lid,pw=body.login_id.strip(),body.password.strip()
    with open(HR_CREDS,encoding='utf-8-sig',newline='') as f:
        for r in csv.DictReader(f):
            if r['login_id']==lid and r['password']==pw:
                t=secrets.token_urlsafe(32); SESSIONS[t]={'role':'hr','user_id':lid,'name':r.get('name','HR')}
                audit('hr',lid,'login'); return {'success':True,'token':t,'role':'hr','user':SESSIONS[t]}
    with open(EMP_CREDS,encoding='utf-8-sig',newline='') as f:
        for r in csv.DictReader(f):
            if r['login_id']==lid and r['password']==pw:
                t=secrets.token_urlsafe(32); SESSIONS[t]={'role':'employee','employee_id':r['employee_id']}
                audit('employee',r['employee_id'],'login'); return {'success':True,'token':t,'role':'employee','user':SESSIONS[t]}
    raise HTTPException(401,'Invalid login ID or password')

@app.post('/api/logout')
def logout(s=session):
    audit(s['role'],s.get('employee_id') or s.get('user_id',''),'logout')
    return {'success':True}

# -------- employee --------
def task_dict(r):
    d=dict(r); d['is_overdue']=bool(d.get('is_overdue')); d['task_order']=int(d.get('task_order') or 0); return d

def employee_payload(eid):
    c=db_conn(); e=c.execute('SELECT * FROM employees WHERE employee_id=?',(eid,)).fetchone(); rows=[task_dict(r) for r in c.execute('SELECT * FROM tasks WHERE employee_id=? ORDER BY task_order',(eid,)).fetchall()]; c.close()
    if not e: raise HTTPException(404,'Employee not found')
    total=len(rows); completed=sum(r['status']=='Completed' for r in rows); pending=sum(r['status']=='Pending' for r in rows)
    actions=[r for r in rows if r['status']=='Pending' and str(r['action_required_by']).lower()=='employee']
    next_task=next((r for r in rows if r['status']=='Pending'),None)
    return {'employee_id':eid,'role':e['role'],'department':e['department'],'joining_date':e['joining_date'],'completion_percent':round(completed/total*100) if total else 0,'completed':completed,'pending':pending,'overdue':sum(r['is_overdue'] for r in rows),'total':total,'action_required_count':len(actions),'action_required':actions,'next_task':next_task,'tasks':rows}

@app.get('/api/employees/me')
def me(s=Depends(emp_session)): return employee_payload(s['employee_id'])

@app.patch('/api/employees/me/tasks/{task_id}')
def employee_update(task_id:str,body:StatusBody,s=Depends(emp_session)):
    if body.status not in ('Completed','Pending'): raise HTTPException(400,'Status must be Completed or Pending')
    eid=s['employee_id']; c=db_conn(); row=c.execute('SELECT * FROM tasks WHERE employee_id=? AND task_id=?',(eid,task_id)).fetchone()
    if not row: c.close(); raise HTTPException(404,'Task not found')
    if body.status=='Completed':
        c.execute("UPDATE tasks SET status='Completed',evidence=?,employee_action='No employee action',is_overdue=0,reviewer_action='',review_timestamp=? WHERE employee_id=? AND task_id=?",('Marked complete in employee portal',now(),eid,task_id))
        event='task_completed'
    else:
        c.execute("UPDATE tasks SET status='Pending',review_timestamp=? WHERE employee_id=? AND task_id=?",(now(),eid,task_id)); event='task_reopened'
    c.commit(); c.close(); audit('employee',eid,event,eid,task_id); return {'success':True,'status':body.status}

@app.post('/api/employee/onboarding/query')
def onboarding_query(body:QueryBody,s=Depends(emp_session)):
    q=body.query.strip(); row,score,band,review=match_query(s['employee_id'],q)
    c=db_conn(); task=c.execute('SELECT * FROM tasks WHERE employee_id=? AND task_id=?',(s['employee_id'],row['task_id'])).fetchone()
    if not task: c.close(); raise HTTPException(404,'Matched task not found for employee')
    t=dict(task); ts=now(); status='Pending Review'
    cur=c.execute('''INSERT INTO onboarding_queries(employee_id,query,matched_guide_id,matched_task_id,matched_task_name,category,similarity_score,confidence_band,requires_human_review,status,is_overdue,approved_next_step,next_step_owner,source_reference,created_at)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(s['employee_id'],q,row['guide_id'],row['task_id'],row['task_name'],row['category'],score,band,review,status,int(t['is_overdue']),row['approved_next_step'],row['next_step_owner'],row['source_reference'],ts))
    qid=cur.lastrowid; c.commit(); c.close(); audit('employee',s['employee_id'],'onboarding_query',s['employee_id'],row['task_id'],qid,f'similarity={score}')
    return {'success':True,'result':{'id':qid,'query':q,'employeeId':s['employee_id'],'matchedGuideId':row['guide_id'],'matchedTaskId':row['task_id'],'matchedTaskName':row['task_name'],'category':row['category'],'similarityScore':score,'confidenceBand':band,'requiresHumanReview':bool(review),'status':t['status'],'isOverdue':bool(t['is_overdue']),'approvedNextStep':row['approved_next_step'],'nextStepOwner':row['next_step_owner'],'sourceReference':row['source_reference'],'createdAt':ts}}

@app.get('/api/employee/onboarding/query-history')
def employee_query_history(s=Depends(emp_session)):
    c=db_conn(); rows=[dict(r) for r in c.execute('SELECT * FROM onboarding_queries WHERE employee_id=? ORDER BY id DESC LIMIT 30',(s['employee_id'],)).fetchall()]; c.close(); return {'items':rows}

# -------- grounded explanation --------
def deterministic_grounded_explanation(d):
    return (
        f"The employee query matches the approved onboarding task "
        f"'{d['matched_task_name']}' with a semantic similarity of {float(d['similarity_score']):.2f}. "
        f"The current task status is '{d['task_status']}'. "
        f"The approved next step is '{d['approved_next_step']}', owned by {d['next_step_owner']}."
    )

def generate_grounded_explanation(d):
    prompt = f"""You are an onboarding assistant for HR.
Use ONLY the facts below. Do not invent policy, deadlines, actions, or employee information.

Employee query: {d['query']}
Matched onboarding task: {d['matched_task_name']}
Category: {d['category']}
Task description: {d['task_description']}
Current status: {d['task_status']}
Due date: {d['due_date']}
Action required by: {d['action_required_by']}
Approved next step: {d['approved_next_step']}
Next step owner: {d['next_step_owner']}
Semantic similarity: {float(d['similarity_score']):.2f}
Confidence band: {d['confidence_band']}

Write a concise 2-4 sentence explanation for an HR reviewer. Explain why this task was matched and what the approved next step is. Do not describe the score as a probability."""
    key=os.getenv('GROQ_API_KEY','').strip()
    if not key:
        return deterministic_grounded_explanation(d), False
    try:
        r=requests.post(
            GROQ_API_URL,
            headers={'Authorization':f'Bearer {key}','Content-Type':'application/json'},
            json={
                'model':GROQ_MODEL,
                'messages':[
                    {'role':'system','content':'Return only the grounded HR explanation.'},
                    {'role':'user','content':prompt}
                ],
                'temperature':0.1,
                'max_tokens':180
            },
            timeout=25
        )
        r.raise_for_status()
        content=r.json()['choices'][0]['message']['content'].strip()
        return content, True
    except Exception:
        return deterministic_grounded_explanation(d), False

# -------- HR --------

# Live employee directory and checklist APIs for HR.
@app.get('/api/hr/employees')
def hr_employees(search: Optional[str] = Query(default=None), s=Depends(hr_session)):
    c = db_conn()
    base = '''
        SELECT e.*,
               COUNT(t.task_id) AS total_tasks,
               SUM(CASE WHEN t.status='Completed' THEN 1 ELSE 0 END) AS completed_tasks,
               SUM(CASE WHEN t.status='Pending' THEN 1 ELSE 0 END) AS pending_tasks,
               SUM(CASE WHEN t.status='Pending' AND t.is_overdue=1 THEN 1 ELSE 0 END) AS overdue_tasks
          FROM employees e
          LEFT JOIN tasks t ON e.employee_id=t.employee_id
    '''
    params = []
    if search:
        term = f"%{search.strip()}%"
        base += " WHERE e.employee_id LIKE ? OR e.role LIKE ? OR e.department LIKE ?"
        params = [term, term, term]
    base += " GROUP BY e.employee_id ORDER BY e.employee_id"
    rows = c.execute(base, params).fetchall()
    items=[]
    for r in rows:
        d=dict(r)
        total=int(d.get('total_tasks') or 0)
        completed=int(d.get('completed_tasks') or 0)
        d['completion_percent']=round(completed/total*100) if total else 0
        for k in ('total_tasks','completed_tasks','pending_tasks','overdue_tasks'):
            d[k]=int(d.get(k) or 0)
        items.append(d)
    c.close()
    return {'items': items, 'total': len(items)}

@app.post('/api/hr/employees')
def hr_create_employee(body: CreateEmployeeBody, s=Depends(hr_session)):
    email=body.email.strip()
    if not EMAIL_RE.match(email): raise HTTPException(400,'Invalid email address')
    try: datetime.fromisoformat(body.joining_date)
    except ValueError: raise HTTPException(400,'Joining date must be YYYY-MM-DD')
    c=db_conn()
    if c.execute('SELECT 1 FROM employees WHERE lower(email)=lower(?)',(email,)).fetchone():
        c.close(); raise HTTPException(409,'An employee with this email already exists')
    role,department=body.role.strip(),body.department.strip()
    guide=c.execute('SELECT * FROM guides WHERE lower(role)=lower(?) ORDER BY task_id LIMIT 1',(role,)).fetchone()
    if not guide: guide=c.execute('SELECT * FROM guides WHERE lower(department)=lower(?) ORDER BY task_id LIMIT 1',(department,)).fetchone()
    if not guide: c.close(); raise HTTPException(400,'No onboarding guide found for this role or department')
    nums=[]
    for (eid0,) in c.execute("SELECT employee_id FROM employees WHERE employee_id LIKE 'EMP-%'").fetchall():
        m=re.match(r'EMP-(\d+)$',eid0)
        if m: nums.append(int(m.group(1)))
    n=(max(nums) if nums else 0)+1; eid=f'EMP-{n:03d}'; login_id=f'EMP{n:04d}'; password=secrets.token_urlsafe(6)
    c.execute('INSERT INTO employees(employee_id,role,department,seniority,job_family,joining_date,guide_id,email) VALUES(?,?,?,?,?,?,?,?)',(eid,role,department,'New Hire',role,body.joining_date,guide['guide_id'],email))
    rows=c.execute('SELECT * FROM guides WHERE guide_id=? ORDER BY task_id',(guide['guide_id'],)).fetchall()
    from datetime import date,timedelta
    jd=date.fromisoformat(body.joining_date)
    for i,r in enumerate(rows,1):
        due=(jd+timedelta(days=i-1)).isoformat()
        c.execute("INSERT INTO tasks(employee_id,task_id,guide_id,task_order,phase,category,task_name,task_description,task_owner,action_required_by,required,status,joining_date,due_date,is_overdue,evidence,issue_text,approved_next_step,next_step_owner,employee_action,reminder_template,escalation_rule,reviewer_action,review_timestamp) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(eid,r['task_id'],r['guide_id'],i,r['phase'],r['category'],r['task_name'],r['task_description'],r['task_owner'],r['action_required_by'],r['required'],'Pending',body.joining_date,due,0,'','',r['approved_next_step'],r['next_step_owner'],'',r['reminder_template'],r['escalation_rule'],'',''))
    fields=['employee_id','login_id','password','email','name']; old=[]
    if EMP_CREDS.exists():
        with open(EMP_CREDS,encoding='utf-8-sig',newline='') as f: rr=csv.DictReader(f); old=list(rr); fields=rr.fieldnames or fields
    for x in ['email','name']:
        if x not in fields: fields.append(x)
    old.append({'employee_id':eid,'login_id':login_id,'password':password,'email':email,'name':body.name.strip()})
    with open(EMP_CREDS,'w',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(old)
    c.commit(); c.close(); audit('hr',s['user_id'],'employee_created',eid,details=f'{body.name.strip()} · {role} · {department}')
    sent=email_service.send_task_assignment_email(employee_email=email,employee_id=eid,role=role,department=department,login_id=login_id,task_rows=[{'task_name':r['task_name'],'due_date':(jd+timedelta(days=i-1)).isoformat(),'action_required_by':r['action_required_by']} for i,r in enumerate(rows,1)])
    return {'success':True,'employee_id':eid,'login_id':login_id,'temporary_password':password,'email':email,'email_sent':sent,'tasks_created':len(rows)}

@app.get('/api/hr/employees/{employee_id}')
def hr_employee(employee_id: str, s=Depends(hr_session)):
    c=db_conn()
    e=c.execute('SELECT * FROM employees WHERE employee_id=?',(employee_id,)).fetchone()
    if not e:
        c.close()
        raise HTTPException(404,'Employee not found')
    rows=[task_dict(r) for r in c.execute(
        'SELECT * FROM tasks WHERE employee_id=? ORDER BY task_order',(employee_id,)
    ).fetchall()]
    qrows=[dict(r) for r in c.execute(
        'SELECT id,query,matched_task_name,similarity_score,confidence_band,status,created_at,reviewer_action,reviewer_note,reviewed_at '
        'FROM onboarding_queries WHERE employee_id=? ORDER BY id DESC LIMIT 20',(employee_id,)
    ).fetchall()]
    c.close()
    total=len(rows)
    completed=sum(r['status']=='Completed' for r in rows)
    pending=sum(r['status']=='Pending' for r in rows)
    return {
        'employee_id': employee_id,
        'role': e['role'],
        'department': e['department'],
        'seniority': e['seniority'],
        'job_family': e['job_family'],
        'joining_date': e['joining_date'],
        'email': e['email'],
        'completion_percent': round(completed/total*100) if total else 0,
        'completed': completed,
        'pending': pending,
        'overdue': sum(r['is_overdue'] for r in rows if r['status']=='Pending'),
        'total': total,
        'tasks': rows,
        'queries': qrows
    }

@app.patch('/api/hr/employees/{employee_id}/email')
def hr_set_employee_email(employee_id:str, body:EmailBody, s=Depends(hr_session)):
    email=body.email.strip()
    if not EMAIL_RE.match(email): raise HTTPException(400,'Invalid email address')
    c=db_conn(); e=c.execute('SELECT employee_id FROM employees WHERE employee_id=?',(employee_id,)).fetchone(); c.close()
    if not e: raise HTTPException(404,'Employee not found')
    set_employee_email(employee_id, email)
    audit('hr',s['user_id'],'employee_email_updated',employee_id,details=f'email set to {email}')
    return {'success':True,'employee_id':employee_id,'email':email}

@app.post('/api/hr/employees/{employee_id}/send-welcome-email')
def hr_send_welcome_email(employee_id:str, s=Depends(hr_session)):
    """Manually (re)send the onboarding task-assignment email for an employee."""
    c=db_conn()
    e=c.execute('SELECT * FROM employees WHERE employee_id=?',(employee_id,)).fetchone()
    rows=[dict(r) for r in c.execute('SELECT * FROM tasks WHERE employee_id=? ORDER BY task_order',(employee_id,)).fetchall()]
    c.close()
    if not e: raise HTTPException(404,'Employee not found')
    email=(e['email'] or '').strip()
    if not email: raise HTTPException(400,'No email on file for this employee')
    login_id=get_login_id(employee_id)
    sent=email_service.send_task_assignment_email(
        employee_email=email, employee_id=employee_id, role=e['role'], department=e['department'],
        login_id=login_id, task_rows=[{'task_name':r['task_name'],'due_date':r['due_date'],'action_required_by':r['action_required_by']} for r in rows])
    audit('hr',s['user_id'],'welcome_email_sent',employee_id,details=f'smtp_sent={sent}')
    return {'success':True,'email':email,'email_sent':sent,'tasks_included':len(rows)}

@app.post('/api/hr/employees/{employee_id}/send-reminder')
def hr_send_manual_reminder(employee_id:str, s=Depends(hr_session)):
    """Optional manual trigger (e.g. for testing) — reminders otherwise go out
    automatically on a schedule without any HR action."""
    c=db_conn()
    e=c.execute('SELECT * FROM employees WHERE employee_id=?',(employee_id,)).fetchone()
    rows=[dict(r) for r in c.execute("SELECT * FROM tasks WHERE employee_id=? AND status!='Completed'",(employee_id,)).fetchall()]
    c.close()
    if not e: raise HTTPException(404,'Employee not found')
    if not rows: raise HTTPException(404,'No pending tasks found for that employee')
    email=(e['email'] or '').strip()
    if not email: raise HTTPException(400,'No email on file for this employee')
    overdue=[{'task_name':r['task_name'],'due_date':r['due_date']} for r in rows if r['is_overdue']]
    due_soon=[{'task_name':r['task_name'],'due_date':r['due_date']} for r in rows if not r['is_overdue']]
    sent=email_service.send_task_reminder_email(email, employee_id, due_soon, overdue)
    audit('hr',s['user_id'],'manual_reminder_sent',employee_id,details=f'smtp_sent={sent}')
    return {'success':True,'email':email,'email_sent':sent,'overdue_count':len(overdue),'due_soon_count':len(due_soon)}

@app.get('/api/hr/stats')
def hr_stats(s=Depends(hr_session)):
    c=db_conn();
    total=c.execute('SELECT COUNT(*) FROM employees').fetchone()[0]
    completed=c.execute("SELECT COUNT(*) FROM tasks WHERE status='Completed'").fetchone()[0]
    pending=c.execute("SELECT COUNT(*) FROM tasks WHERE status='Pending'").fetchone()[0]
    cases=c.execute('SELECT COUNT(*) FROM onboarding_queries').fetchone()[0]
    review=c.execute("SELECT COUNT(*) FROM onboarding_queries WHERE status='Pending Review'").fetchone()[0]
    overdue=c.execute("SELECT COUNT(*) FROM tasks WHERE is_overdue=1 AND status='Pending'").fetchone()[0]
    employees_attention=c.execute("SELECT COUNT(DISTINCT employee_id) FROM tasks WHERE status='Pending' AND (is_overdue=1 OR action_required_by='Employee')").fetchone()[0]
    c.close(); return {'employees':total,'completed_tasks':completed,'pending_tasks':pending,'cases':cases,'pending_review':review,'overdue':overdue,'employees_attention':employees_attention}

@app.get('/api/hr/cases')
def hr_cases(status:Optional[str]=Query(default=None),s=Depends(hr_session)):
    c=db_conn(); base='''SELECT q.*, t.status AS task_status, t.due_date, t.action_required_by, t.task_owner, t.employee_action, e.role, e.department FROM onboarding_queries q JOIN tasks t ON q.employee_id=t.employee_id AND q.matched_task_id=t.task_id JOIN employees e ON q.employee_id=e.employee_id WHERE 1=1'''; params=[]
    if status: base+=' AND q.status=?'; params.append(status)
    base+=' ORDER BY q.id DESC'; rows=[dict(r) for r in c.execute(base,params).fetchall()]; c.close(); return {'items':rows}

@app.get('/api/hr/cases/{qid}')
def hr_case(qid:int,s=Depends(hr_session)):
    c=db_conn(); row=c.execute('''SELECT q.*, t.status AS task_status, t.due_date, t.action_required_by, t.task_owner, t.employee_action, t.task_description, t.evidence, e.role, e.department FROM onboarding_queries q JOIN tasks t ON q.employee_id=t.employee_id AND q.matched_task_id=t.task_id JOIN employees e ON q.employee_id=e.employee_id WHERE q.id=?''',(qid,)).fetchone(); c.close()
    if not row: raise HTTPException(404,'Case not found')
    d=dict(row)
    d['grounded_explanation']=d.get('grounded_explanation') or deterministic_grounded_explanation(d)
    d['explanation_source']='groq' if d.get('grounded_explanation') and os.getenv('GROQ_API_KEY') else 'approved-guide'
    return d

@app.post('/api/hr/cases/{qid}/explain')
def hr_case_explain(qid:int,s=Depends(hr_session)):
    c=db_conn(); row=c.execute('''SELECT q.*, t.status AS task_status, t.due_date, t.action_required_by, t.task_owner, t.task_description, e.role, e.department
                                  FROM onboarding_queries q
                                  JOIN tasks t ON q.employee_id=t.employee_id AND q.matched_task_id=t.task_id
                                  JOIN employees e ON q.employee_id=e.employee_id
                                  WHERE q.id=?''',(qid,)).fetchone()
    if not row:
        c.close(); raise HTTPException(404,'Case not found')
    d=dict(row)
    explanation, used_groq=generate_grounded_explanation(d)
    ts=now()
    c.execute('UPDATE onboarding_queries SET grounded_explanation=? WHERE id=?',(explanation,qid))
    c.commit(); c.close()
    audit('hr',s['user_id'],'grounded_explanation_generated',d['employee_id'],d['matched_task_id'],qid,f'source={"Groq" if used_groq else "approved-guide-fallback"}')
    return {'success':True,'groundedExplanation':explanation,'source':'Groq' if used_groq else 'Approved guide fallback'}

@app.post('/api/hr/cases/{qid}/review')
def hr_review(qid:int,body:ReviewBody,s=Depends(hr_session)):
    c=db_conn(); row=c.execute('SELECT * FROM onboarding_queries WHERE id=?',(qid,)).fetchone()
    if not row: c.close(); raise HTTPException(404,'Case not found')
    status='Approved' if body.action=='approve' else 'Overridden'; ts=now()
    c.execute('UPDATE onboarding_queries SET status=?,reviewer_action=?,reviewer_note=?,reviewed_at=? WHERE id=?',(status,body.action,body.note,ts,qid))
    c.commit(); c.close(); audit('hr',s['user_id'],'case_review',row['employee_id'],row['matched_task_id'],qid,f'{body.action}: {body.note}')
    return {'success':True,'status':status,'reviewedAt':ts}

@app.get('/api/hr/audit')
def hr_audit(limit:int=200,s=Depends(hr_session)):
    c=db_conn(); rows=[dict(r) for r in c.execute('SELECT * FROM audit_log ORDER BY id DESC LIMIT ?',(limit,)).fetchall()]; c.close(); return {'items':rows}

@app.get('/api/hr/review-queue')
def hr_queue(s=Depends(hr_session)):
    c=db_conn(); rows=[dict(r) for r in c.execute("SELECT q.id,q.employee_id,q.query,q.matched_task_name,q.similarity_score,q.confidence_band,q.created_at,e.department FROM onboarding_queries q JOIN employees e ON q.employee_id=e.employee_id WHERE q.status='Pending Review' ORDER BY q.id DESC").fetchall()]; c.close(); return {'items':rows}
