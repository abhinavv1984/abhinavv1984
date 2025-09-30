import os
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
import logging
import asyncio

from fastapi import FastAPI, Depends, HTTPException, status, Response, Request, Form
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from jose import JWTError, jwt
from passlib.context import CryptContext
import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

# Local imports
from scripts.QA_Main_v3 import run_once
from scripts.QA_HTML_V2 import fetch_report_data_from_db

# Allow running even if scripts.db_service isn't present in this environment
try:
    from scripts.db_service import AsyncDatabaseService
except Exception:  # pragma: no cover - fallback for local runs
    from async_database_service import AsyncDatabaseService


pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def load_config(config_file: str = "config.yaml") -> dict:
    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.error(f"Configuration file '{config_file}' not found.")
        raise
    except yaml.YAMLError as e:
        logger.error(f"Error parsing configuration file '{config_file}': {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error loading configuration file: {e}")
        raise


def get_auth_settings():
    config = load_config()
    auth = (config.get('auth') or {})
    secret_key = os.getenv('AUTH_SECRET', auth.get('secret_key') or 'change-me-in-env')
    expire_minutes = int(os.getenv('AUTH_EXPIRE_MINUTES', auth.get('access_token_expire_minutes') or 120))
    return secret_key, expire_minutes, config


def hash_password_if_needed(password_value: str) -> str:
    # If value looks like a bcrypt hash, keep it; otherwise hash now
    if password_value.startswith('$2b$') or password_value.startswith('$2a$'):
        return password_value
    return pwd_context.hash(password_value)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    if hashed_password.startswith('$2'):
        return pwd_context.verify(plain_password, hashed_password)
    # If stored as plaintext (not recommended), fallback compare
    return plain_password == hashed_password


ALGORITHM = "HS256"


def create_access_token(data: dict, secret_key: str, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=120))
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, secret_key, algorithm=ALGORITHM)
    return encoded_jwt


async def get_current_user(request: Request):
    secret_key, expire_minutes, config = get_auth_settings()
    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    try:
        payload = jwt.decode(token, secret_key, algorithms=[ALGORITHM])
        username = payload.get("sub")
        role = payload.get("role", "user")
        if not username:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token: Missing 'sub'")

        # Verify user still exists in database
        user: Optional[Dict[str, Any]] = None
        try:
            async with AsyncDatabaseService(config) as db_service:
                user = await db_service.get_user_by_username(username)
                if not user or not user.get('is_active', True):
                    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")
        except HTTPException:
            # Propagate auth errors
            raise
        except Exception as e:
            logger.error(f"Database error: {e}")
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Database error")

        return {"username": username, "role": role, "user_info": user}
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


async def require_admin(user = Depends(get_current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin required")
    return user


app = FastAPI(title="QA Automation Web")

# Scheduler instance
scheduler = AsyncIOScheduler()
_job_added = False

# CORS (adjust as needed)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files for gallery (logo, favicon, etc.)
base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
app.mount("/static", StaticFiles(directory=os.path.join(base_dir, "gallery")), name="static")


@app.get("/", response_class=HTMLResponse)
def login_page(request: Request):
    # If already authenticated, go straight to report
    try:
        secret_key, expire_minutes, _ = get_auth_settings()
        token = request.cookies.get("access_token")
        if token:
            jwt.decode(token, secret_key, algorithms=[ALGORITHM])
            return RedirectResponse(url="/report", status_code=303)
    except Exception:
        pass
    # Otherwise show login page
    return """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>QA Automation - Login</title>
    <link rel="icon" href="/static/favicon.ico" type="image/x-icon">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }
        .login-container {
            background: white;
            border-radius: 16px;
            box-shadow: 0 20px 40px rgba(0,0,0,0.1);
            padding: 40px;
            width: 100%;
            max-width: 400px;
            position: relative;
            overflow: hidden;
        }
        .login-container::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 4px;
            background: linear-gradient(90deg, #667eea, #764ba2);
        }
        .logo {
            text-align: center;
            margin-bottom: 30px;
        }
        .logo img {
            height: 60px;
            margin-bottom: 10px;
        }
        .logo h1 {
            color: #2d3748;
            font-size: 24px;
            font-weight: 600;
        }
        .logo p {
            color: #718096;
            font-size: 14px;
            margin-top: 5px;
        }
        .form-group {
            margin-bottom: 20px;
        }
        .form-group label {
            display: block;
            margin-bottom: 8px;
            color: #4a5568;
            font-weight: 500;
            font-size: 14px;
        }
        .form-group input {
            width: 100%;
            padding: 12px 16px;
            border: 2px solid #e2e8f0;
            border-radius: 8px;
            font-size: 16px;
            transition: all 0.3s ease;
            background: #f7fafc;
        }
        .form-group input:focus {
            outline: none;
            border-color: #667eea;
            background: white;
            box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
        }
        .login-btn {
            width: 100%;
            padding: 12px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            border-radius: 8px;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s ease;
            margin-top: 10px;
        }
        .login-btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 10px 20px rgba(102, 126, 234, 0.3);
        }
        .login-btn:active {
            transform: translateY(0);
        }
        .error-msg {
            color: #e53e3e;
            font-size: 14px;
            margin-top: 10px;
            padding: 8px 12px;
            background: #fed7d7;
            border: 1px solid #feb2b2;
            border-radius: 6px;
            display: none;
        }
        .loading {
            display: none;
            text-align: center;
            margin-top: 10px;
        }
        .spinner {
            border: 2px solid #f3f3f3;
            border-top: 2px solid #667eea;
            border-radius: 50%;
            width: 20px;
            height: 20px;
            animation: spin 1s linear infinite;
            margin: 0 auto;
        }
        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }
        .footer {
            text-align: center;
            margin-top: 30px;
            color: #718096;
            font-size: 12px;
        }
    </style>
    
</head>
<body>
    <div class="login-container">
        <div class="logo">
            <img src="/static/logo.png" alt="QA Automation" onerror="this.style.display='none'">
            <h1>QA Automation</h1>
            <p>Quality Assurance Testing Platform</p>
        </div>
        
        <form id="loginForm">
            <div class="form-group">
                <label for="username">Username</label>
                <input type="text" id="username" name="username" required autocomplete="username">
            </div>
            
            <div class="form-group">
                <label for="password">Password</label>
                <input type="password" id="password" name="password" required autocomplete="current-password">
            </div>
            
            <button type="submit" class="login-btn">Sign In</button>
            
            <div class="loading" id="loading">
                <div class="spinner"></div>
                <p>Signing in...</p>
            </div>
            
            <div class="error-msg" id="errorMsg"></div>
        </form>
        
        <div class="footer">
            <p>&copy; 2025 OTA Group. All rights reserved.</p>
        </div>
    </div>

    <script>
        document.getElementById('loginForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            
            const form = e.target;
            const loading = document.getElementById('loading');
            const errorMsg = document.getElementById('errorMsg');
            const submitBtn = form.querySelector('.login-btn');
            
            // Show loading state
            loading.style.display = 'block';
            errorMsg.style.display = 'none';
            submitBtn.disabled = true;
            submitBtn.textContent = 'Signing In...';
            
            try {
                const formData = new FormData(form);
                const response = await fetch('/login', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/x-www-form-urlencoded',
                    },
                    body: new URLSearchParams(formData)
                });
                
                if (response.ok) {
                    window.location.href = '/report';
                } else {
                    const errorText = await response.text();
                    errorMsg.textContent = errorText || 'Login failed. Please check your credentials.';
                    errorMsg.style.display = 'block';
                }
            } catch (error) {
                errorMsg.textContent = 'Network error. Please try again.';
                errorMsg.style.display = 'block';
            } finally {
                loading.style.display = 'none';
                submitBtn.disabled = false;
                submitBtn.textContent = 'Sign In';
            }
        });
    </script>
</body>
</html>
"""


@app.post("/login")
async def login(response: Response, username: str = Form(...), password: str = Form(...)):
    secret_key, expire_minutes, config = get_auth_settings()
    
    # Authenticate against database
    db_service = AsyncDatabaseService(config)
    user = await db_service.authenticate_user(username, password)
    
    if not user:
        return Response(content="Invalid credentials", status_code=401)
    
    role = user.get('role', 'user')
    token = create_access_token({"sub": username, "role": role}, secret_key, timedelta(minutes=expire_minutes))
    
    # Set HTTP-only cookie
    response = RedirectResponse(url="/report", status_code=303)
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        secure=True,  # Set to True for production
        samesite="lax",
        max_age=expire_minutes * 60
    )
    return response


@app.get("/logout")
def logout_get(request: Request):
    # Always redirect to home (login)
    resp = RedirectResponse(url="/", status_code=303)
    resp.delete_cookie("access_token")
    return resp


@app.post("/logout")
def logout_post(request: Request):
    resp = RedirectResponse(url="/", status_code=303)
    resp.delete_cookie("access_token")
    return resp


@app.get("/me")
async def me(user = Depends(get_current_user)):
    return user


def get_reports_dir() -> str:
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    return os.path.join(base_dir, 'reports')


@app.get("/report")
async def report(user = Depends(get_current_user)):
    reports_dir = get_reports_dir()
    html_path = os.path.join(reports_dir, 'QATestOutput.html')
    if not os.path.isfile(html_path):
        raise HTTPException(status_code=404, detail="Report not found. Run the job to generate it.")
    
    # Read and modify HTML to fix static paths
    with open(html_path, 'r', encoding='utf-8') as f:
        html_content = f.read()
    
    # Fix static file paths
    html_content = html_content.replace('../gallery/', '/static/')

    # Inject signed-in username and Sign Out link into the page header
    try:
        username = (user or {}).get('username') or 'User'
        profile_bar = f"""
<div style="position:sticky;top:0;z-index:1000;background:#111827;color:#fff;padding:8px 12px;display:flex;justify-content:space-between;align-items:center;">
  <div style="font-weight:600;">Signed in as: {username}</div>
  <a href="/logout" style="color:#fff;background:#ef4444;padding:6px 10px;border-radius:6px;text-decoration:none;font-weight:600;">Sign Out</a>
</div>
"""
        # Auto refresh from config
        _, _, cfg = get_auth_settings()
        auto_refresh_seconds = int(((cfg.get('schedule') or {}).get('auto_refresh_seconds')) or 0)
        refresh_snippet = ''
        if auto_refresh_seconds and auto_refresh_seconds > 0:
            refresh_snippet = f"\n<script>setTimeout(function(){{ location.reload(); }},{auto_refresh_seconds*1000});</script>\n"
        html_content = html_content.replace('<body>', f'<body>\n{profile_bar}{refresh_snippet}', 1)
    except Exception:
        pass
    
    return HTMLResponse(content=html_content)


@app.get("/reports/{path:path}")
async def report_files(path: str, user = Depends(get_current_user)):
    reports_dir = get_reports_dir()
    full_path = os.path.abspath(os.path.join(reports_dir, path))
    if not os.path.commonpath([reports_dir, full_path]) == reports_dir:
        raise HTTPException(status_code=403, detail="Forbidden")
    if not os.path.isfile(full_path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(full_path)


@app.post("/run")
async def run_now(user = Depends(require_admin)):
    # Trigger a fresh run and return summary
    result = run_once("config.yaml")
    return JSONResponse(result)


# API alias to run-now for external callers
@app.post("/api/run")
async def api_run_now(user = Depends(require_admin)):
    result = run_once("config.yaml")
    return JSONResponse(result)


# API to fetch current report data directly from database as JSON
@app.get("/api/report/data")
async def api_report_data(user = Depends(get_current_user)):
    _, _, config = get_auth_settings()
    try:
        data = fetch_report_data_from_db(config, [])
        return JSONResponse({"ok": True, "count": len(data), "data": data})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": f"Value error: {e}"}, status_code=400)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# API to return the current HTML report content
@app.get("/api/report/html")
async def api_report_html(user = Depends(get_current_user)):
    reports_dir = get_reports_dir()
    html_path = os.path.join(reports_dir, 'QATestOutput.html')
    if not os.path.isfile(html_path):
        raise HTTPException(status_code=404, detail="Report not found. Run the job to generate it.")
    with open(html_path, 'r', encoding='utf-8') as f:
        html_content = f.read()
    # Keep static fix consistent
    html_content = html_content.replace('../gallery/', '/static/')
    return HTMLResponse(content=html_content)


async def run_job():
    try:
        await asyncio.to_thread(run_once, "config.yaml")
    except Exception as e:
        logger.error(f"Error running job: {e}")


@app.on_event("startup")
async def startup_event():
    global _job_added
    secret_key, expire_minutes, cfg = get_auth_settings()
    sched_cfg = (cfg.get('schedule') or {})
    enabled = bool(sched_cfg.get('enabled', True))
    interval_minutes = int(sched_cfg.get('interval_minutes', 30))
    if enabled and interval_minutes > 0 and not _job_added:
        # Add job (pass coroutine function directly for AsyncIOScheduler)
        scheduler.add_job(
            run_job,
            trigger=IntervalTrigger(minutes=interval_minutes),
            id="qa_periodic_run",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        scheduler.start()
        _job_added = True


@app.on_event("shutdown")
async def shutdown_event():
    if scheduler.running:
        scheduler.shutdown(wait=False)

