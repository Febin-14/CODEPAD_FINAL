# Task Delegation Tool

## Overview
A task delegation and code management platform designed for project managers and developers. It features role-based access, an integrated code editor, and persistent storage using MongoDB.

## Tech Stack
- **Backend**: FastAPI (Python)
- **Database**: MongoDB (Motor async driver)
- **Frontend**: HTML, CSS, JavaScript (Jinja2 Templates)
- **Editor**: Monaco Editor (VS Code's editor engine)

## Setup Instructions

### 1. Prerequisites
- Python 3.8+
- MongoDB installed and running locally

### 2. Install dependencies
```bash
cd project
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Start MongoDB
Ensure MongoDB is running locally on the default port (27017).

### 4. Run FastAPI server
```bash
uvicorn app.main:app --reload
```

### 5. (Optional) AI Features
- Set `GEMINI_API_KEY` in your environment to use the Google Gemini AI Code Assistant in the editor.
- Set `OPENAI_API_KEY` in your environment to use AI-based automatic task assignment and voice chat features. If unset, tasks are assigned using a simple keyword match (e.g. “frontend”/“UI” → frontend developer).

### 6. (Optional) GitHub – push approved task code to a repo
When you approve a task, the app can push the submitted code to a GitHub repository (under a folder named by task, e.g. `tasks/add-login-page-abc123/`) instead of only storing it locally.

**Setup:**

1. **Create a GitHub Personal Access Token**
   - GitHub → **Settings** → **Developer settings** → **Personal access tokens** → **Tokens (classic)**.
   - **Generate new token (classic)**. Give it a name (e.g. "Task Delegation Tool").
   - Enable the **`repo`** scope (full control of private repositories).
   - Generate and copy the token (you won't see it again).

2. **Create or choose a repository**
   - create a new one (e.g. `my-company/coding-tasks`). The app will create a `tasks/` folder and subfolders per task automatically.

3. **Add GitHub config to `.env`**
   - In the `project/app` directory, create or edit `.env` and add (use your repo and token; no spaces around `=` is recommended):

   ```env
   GITHUB_REPO=owner/repo-name
   GITHUB_TOKEN=ghp_your_token_here
   ```

   Example:
   ```env
   GITHUB_REPO=myusername/coding-ide-repo
   GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx
   ```

4. **Run the app**
   - With `GITHUB_REPO` and `GITHUB_TOKEN` set, approving a task will push that task's code and metadata to the repo under `tasks/<task-slug>/` (e.g. `code.html` or `code.py` plus `metadata.txt`). The manager dashboard shows a **View on GitHub** link for completed tasks.

If either variable is missing, the app still works; approved tasks are marked done but code is not pushed to GitHub.

### 7. Access the app
- Open your browser and navigate to:

- App URL: `http://localhost:8000`

## Default Credentials:

 - Manager: manager / manager123
 - Frontend Dev: dev1 / dev123
 - Backend Dev: dev2 / dev123

