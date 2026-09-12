
# 🤖 OnboardAI — Intelligent Employee Onboarding Platform

> A dynamic AI-powered employee onboarding system that connects employees and HR through a unified portal, intelligent onboarding assistance, real-time task tracking, HR review workflows, audit logging, and automated email notifications.

---

## 📌 Overview

**OnboardAI** is a full-stack employee onboarding platform designed to simplify and automate the onboarding process for both employees and HR teams.

The system provides two role-based portals:

- 👤 **Employee Portal** — Employees can log in, track onboarding tasks, complete assigned activities, and ask natural-language questions.
- 🧑‍💼 **HR Portal** — HR can manage employees, monitor onboarding progress, review employee queries, approve or override AI-assisted cases, and view audit information.

The platform uses a **FastAPI backend**, **SQLite database**, and **React + Vite frontend**, with a transformer-based semantic search system for intelligent onboarding queries.

---

# ✨ Key Features

## 👤 Employee Portal

### 🔐 Individual Login
Each employee has their own credentials and personalized onboarding workspace.

### 📋 Live Onboarding Checklist
Employees can:

- View assigned onboarding tasks
- Track task status
- See due dates
- Identify overdue tasks
- Complete tasks using the **Complete** button

Task completion is immediately persisted to the SQLite database.

### 🤖 Natural-Language Onboarding Assistant

Employees can ask questions such as:

> "What should I complete before submitting my documents?"

> "Who is responsible for my laptop setup?"

> "What is the next step after completing the induction?"

The system uses:

- Transformer sentence embeddings
- Cosine similarity
- Approved onboarding guide content

to identify the most relevant onboarding information.

### 📝 HR Review Workflow

Employee queries are stored as cases with:

```text
Pending Review
````

HR can later review the query and provide an approved response or override.

---

# 🧑‍💼 HR Portal

## 📊 Dashboard

The HR dashboard provides an overview of the onboarding process, including:

* Total employees
* Overall completion
* Pending tasks
* Overdue tasks
* Employee onboarding progress
* Pending review cases

---

## 👥 Employee Management

HR can view all **500 demo employees**.

Features include:

* Employee search
* Live onboarding completion
* Pending task count
* Overdue task count
* Employee details
* Onboarding progress

---

## 🗂️ Employee Drawer

Selecting an employee opens a detailed view containing:

* Complete onboarding checklist
* Completed tasks
* Remaining tasks
* Task due dates
* Previous employee queries
* Current onboarding status

---

# 📨 Cases & Review Queue

Employee-submitted onboarding questions are automatically stored as cases.

HR can:

1. Open a case
2. View the employee's question
3. View retrieved onboarding guide information
4. Generate an AI-assisted explanation
5. Review the case
6. Approve the response
7. Override the response
8. Add a reviewer note

### Case Lifecycle

```text
Employee Question
       ↓
Pending Review
       ↓
HR Opens Case
       ↓
AI Explanation / Approved Guide
       ↓
HR Review
       ↓
┌─────────────────┐
│ Approve         │
│       OR        │
│ Override        │
└─────────────────┘
       ↓
Reviewer Note
       ↓
Audit Log
```

---

# 🧠 AI & Semantic Search

OnboardAI uses **transformer-based sentence embeddings** instead of relying only on keyword matching.

### Query Processing

```text
Employee Query
      ↓
Text Tokenization
      ↓
Transformer Sentence Embedding
      ↓
Vector Representation
      ↓
Cosine Similarity
      ↓
Approved Guide Documents
      ↓
Most Relevant Onboarding Information
      ↓
Case Stored for HR Review
```

The semantic search system compares the employee's query against approved onboarding guide text.

This allows the system to understand queries that use different wording but have similar meanings.

### Example

Employee asks:

```text
"How do I get my company laptop?"
```

The system can identify relevant guide information such as:

```text
IT equipment collection and laptop allocation procedure
```

even though the exact words may not match.

---

# 🧾 Grounded AI Explanations

HR can click **Generate** inside a case to generate an explanation.

If `GROQ_API_KEY` is configured:

```text
Case + Retrieved Guide Facts
            ↓
        Groq LLM
            ↓
   Grounded Explanation
```

The explanation is generated using the retrieved case and approved guide facts.

If `GROQ_API_KEY` is not configured, the system uses a deterministic approved-guide fallback.

This allows the project to work without requiring an external LLM API.

---

# 📧 Automated Email Notifications

OnboardAI includes an automated email notification system.

## 🔔 Automatic Reminders

A background scheduler automatically checks onboarding tasks and sends reminder emails when:

* A task is overdue
* A task is due soon

No HR action is required.

By default, the scheduler:

```text
Runs once at application startup
        ↓
Checks pending tasks
        ↓
Sends required reminders
        ↓
Runs every 60 minutes
```

### Reminder Configuration

Default values:

```env
REMINDER_LOOKAHEAD_DAYS=2
REMINDER_COOLDOWN_HOURS=24
REMINDER_CHECK_INTERVAL_MINUTES=60
```

This means:

* Tasks due within the next **2 days** are considered "due soon"
* The same employee won't receive reminders more than once within **24 hours**
* The scheduler checks for reminders every **60 minutes**

---

# 📩 HR Email Controls

HR can also manually manage employee email notifications.

### Update Employee Email

```http
PATCH /api/hr/employees/{employee_id}/email
```

### Send Welcome Email

```http
POST /api/hr/employees/{employee_id}/send-welcome-email
```

This sends the employee their onboarding task assignment information.

### Send Immediate Reminder

```http
POST /api/hr/employees/{employee_id}/send-reminder
```

This allows HR to manually trigger an onboarding reminder.

---

# 🧪 Email Demo Mode

A real SMTP server is **not required** to demonstrate the email functionality.

If SMTP is not configured, email messages are written to:

```text
data/email_outbox.log
```

This allows the complete email workflow to be tested during development and demonstrations.

Example:

```text
Employee Task Assignment
        ↓
Email Service
        ↓
SMTP configured?
    ↙          ↘
  YES           NO
   ↓             ↓
Real Email    Outbox Log
```

---

# 📧 Configure Real Email Delivery

Copy:

```text
.env.example
```

to:

```text
.env
```

Then configure your SMTP credentials.

Example:

```env
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your_address@gmail.com
SMTP_PASSWORD=your_app_password
SMTP_FROM_EMAIL=your_address@gmail.com
SMTP_FROM_NAME=OnboardAI
SMTP_USE_TLS=true
EMAIL_NOTIFICATIONS_ENABLED=true
```

### Gmail

If using Gmail, use a **Google App Password** rather than your normal Gmail password.

> ⚠️ Never commit your `.env` file or real SMTP credentials to GitHub.

---

# 🗃️ Database

OnboardAI uses **SQLite** for persistent application state.

The database stores live information such as:

* Employees
* Onboarding tasks
* Task completion status
* Employee queries
* HR reviews
* Audit logs
* Guide information
* Employee email addresses

The CSV files are used as **seed/reference data**, while the live application state is stored in:

```text
onboarding.db
```

---

# 📁 Project Structure

```text
OnboardAI_PROPER_DYNAMIC_v3/
│
├── backend/
│   ├── __init__.py
│   ├── main.py
│   ├── email_service.py
│   ├── scheduler.py
│   └── requirements.txt
│
├── data/
│   ├── employee_login_credentials.csv
│   ├── hr_login_credentials.csv
│   ├── employee_onboarding_tasks.csv
│   └── onboarding_guides_60_roles_detailed.csv
│
├── frontend/
│   ├── src/
│   │   ├── main.jsx
│   │   └── styles.css
│   ├── index.html
│   ├── hr-dashboard.html
│   ├── package.json
│   ├── package-lock.json
│   └── vite.config.js
│
├── .env.example
├── requirements.txt
└── README.md
```

---

# 🛠️ Technology Stack

| Layer          | Technology                      |
| -------------- | ------------------------------- |
| Frontend       | React.js                        |
| Build Tool     | Vite                            |
| Backend        | Python + FastAPI                |
| Database       | SQLite                          |
| AI / NLP       | Transformers                    |
| Embeddings     | Transformer Sentence Embeddings |
| Similarity     | Cosine Similarity               |
| Scheduling     | APScheduler                     |
| Email          | SMTP                            |
| AI Explanation | Groq API                        |
| Seed Data      | CSV                             |
| API Format     | REST                            |

---

# 🔌 API Endpoints

## Authentication

```http
POST /api/login
POST /api/logout
```

---

## Employee

```http
GET   /api/employees/me
PATCH /api/employees/me/tasks/{task_id}

POST  /api/employee/onboarding/query
GET   /api/employee/onboarding/query-history
```

---

## HR Employees

```http
GET   /api/hr/employees
POST  /api/hr/employees
GET   /api/hr/employees/{employee_id}

PATCH /api/hr/employees/{employee_id}/email

POST  /api/hr/employees/{employee_id}/send-welcome-email

POST  /api/hr/employees/{employee_id}/send-reminder
```

---

## HR Dashboard

```http
GET /api/hr/stats
GET /api/hr/cases
GET /api/hr/cases/{qid}
GET /api/hr/review-queue
GET /api/hr/audit
```

---

## HR Case Review

```http
POST /api/hr/cases/{qid}/explain
POST /api/hr/cases/{qid}/review
```

---

## Health

```http
GET /health
GET /api/health
```

---

# 🚀 Installation & Setup

## Prerequisites

Make sure the following are installed:

* Python 3.10+
* Node.js
* npm
* Git

---

# ⚙️ Backend Setup

Open a terminal in the project root.

```powershell
cd backend
```

Create a virtual environment:

```powershell
python -m venv venv
```

Activate it on Windows PowerShell:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
venv\Scripts\Activate.ps1
```

Install dependencies:

```powershell
python -m pip install -r requirements.txt
```

Return to the project root:

```powershell
cd ..
```

Start the FastAPI server:

```powershell
python -m uvicorn backend.main:app --reload
```

The backend will be available at:

```text
http://127.0.0.1:8000
```

API documentation:

```text
http://127.0.0.1:8000/docs
```

---

# 💻 Frontend Setup

Open another terminal.

```powershell
cd frontend
```

Install dependencies:

```powershell
npm.cmd install
```

Start the development server:

```powershell
npm.cmd run dev
```

Vite will provide the frontend URL in the terminal.

---

# 🔑 Demo Credentials

## Employee

```text
Employee ID: EMP001
Password: 123001
```

## HR

```text
HR ID: HR001
Password: hr123
```

Additional demo employees are available in:

```text
data/employee_login_credentials.csv
```

---

# 📊 Demo Data

The project includes seed/reference data for:

* **500 employees**
* Employee login credentials
* HR credentials
* Employee onboarding tasks
* Detailed onboarding guides
* Multiple roles/departments

The CSV files provide the initial dataset used to populate the application database.

---

# 🔄 Application Workflow

## Employee Workflow

```text
Login
  ↓
Employee Dashboard
  ↓
View Onboarding Checklist
  ↓
Complete Tasks
  ↓
Database Updated
  ↓
Ask Onboarding Question
  ↓
Semantic Search
  ↓
Relevant Approved Guide Retrieved
  ↓
Query Stored as Pending Review
  ↓
HR Reviews Case
```

---

## HR Workflow

```text
HR Login
   ↓
HR Dashboard
   ↓
View 500 Employees
   ↓
Monitor Completion
   ↓
Open Employee
   ↓
View Tasks + Queries
   ↓
Open Case
   ↓
Generate Explanation
   ↓
Approve / Override
   ↓
Reviewer Note
   ↓
Audit Log
```

---

# 🔔 Reminder Workflow

```text
Application Startup
        ↓
Scheduler Starts
        ↓
Check Pending Tasks
        ↓
 ┌───────────────┐
 │ Overdue?      │
 │ Due Soon?     │
 └───────────────┘
        ↓
Find Employee Email
        ↓
Send Reminder
        ↓
SMTP / Outbox Log
        ↓
Save Reminder State
        ↓
Wait for Next Interval
```

---

# 🔐 Security Notes

For production deployment:

* Do not commit `.env`
* Never expose SMTP passwords
* Use secure authentication
* Use hashed passwords
* Add proper authorization middleware
* Validate all API inputs
* Use HTTPS
* Restrict CORS
* Use a production-grade database if required
* Store secrets using environment variables or a secret manager

The included credentials are intended for **demo/development purposes**.

---

# 🧪 Testing Without SMTP

You can demonstrate email functionality without configuring a mail server.

1. Start the backend.
2. Login as HR.
3. Update an employee's email if required.
4. Trigger:

```http
POST /api/hr/employees/{employee_id}/send-reminder
```

5. Check:

```text
data/email_outbox.log
```

The generated email information will be recorded there.

---

# 🧠 Why Semantic Search?

Traditional keyword matching may fail when employees use different words for the same concept.

For example:

```text
"Where do I collect my laptop?"
```

and

```text
"How can I get my assigned computer?"
```

contain different keywords but have a similar meaning.

Transformer embeddings convert text into numerical vectors, allowing the system to compare **semantic meaning** rather than only exact words.

Cosine similarity is then used to identify the most relevant approved guide content.

---

# 🎯 Benefits

### For Employees

* Faster access to onboarding information
* Personalized task checklist
* Real-time task status
* Natural-language question answering
* Automatic reminders
* Reduced dependency on HR for routine questions

### For HR

* Centralized employee onboarding management
* Visibility into 500 employees
* Live completion tracking
* Automated overdue monitoring
* Centralized query review
* AI-assisted case explanations
* Manual approve/override controls
* Complete audit trail

### For the Organization

* Reduced manual onboarding work
* Better process visibility
* Consistent onboarding guidance
* Automated communication
* Improved accountability
* Scalable onboarding workflow

---

# 📌 Project Highlights

> **OnboardAI combines task management, semantic AI, human-in-the-loop review, automated communication, and auditability into one unified onboarding platform.**

### Core Architecture

```text
                 ┌─────────────────────┐
                 │     React + Vite    │
                 │      Frontend       │
                 └──────────┬──────────┘
                            │
                            ▼
                 ┌─────────────────────┐
                 │       FastAPI       │
                 │       Backend       │
                 └──────┬──────┬───────┘
                        │      │
              ┌─────────┘      └─────────┐
              ▼                          ▼
      ┌───────────────┐          ┌───────────────┐
      │    SQLite     │          │  AI / NLP     │
      │    Database   │          │  Embeddings   │
      └───────────────┘          └───────┬───────┘
                                         │
                                         ▼
                                  Approved Guides

                        ┌─────────────────────┐
                        │ Background Scheduler│
                        │     APScheduler     │
                        └──────────┬──────────┘
                                   │
                                   ▼
                             Email Service
                              SMTP / Log
```

---

# 📜 License

This project is intended for educational, demonstration, and hackathon purposes.

---



