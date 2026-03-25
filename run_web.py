"""
Web 服务启动入口（供 pythonw.exe 后台运行使用）
"""
import sys
import os

# 确保工作目录和模块路径正确
SERVICE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(SERVICE_DIR)
sys.path.insert(0, SERVICE_DIR)

from main import setup_logging
from models import init_db
from web.app import app
from config import WEB_HOST, WEB_PORT

setup_logging()
init_db()

from waitress import serve
serve(app, host=WEB_HOST, port=WEB_PORT, threads=4)
