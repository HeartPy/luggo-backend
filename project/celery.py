"""
Celery アプリケーションの初期化

非同期ジョブ（例: 日次自動割当）を動かすための Celery アプリを定義する。
Django 設定の CELERY_* を読み込み、各アプリの tasks.py を自動検出する。
"""
import os

from celery import Celery


os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'project.settings')

app = Celery('project')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()
