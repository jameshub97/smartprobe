"""Dashboard and static-asset routes."""

import os
from flask import Blueprint, send_from_directory

dashboard_bp = Blueprint('dashboard', __name__)

_DASHBOARD_DIR = os.path.join(os.path.dirname(__file__), '..', 'dashboard')


@dashboard_bp.route('/')
@dashboard_bp.route('/dashboard')
def dashboard():
    return send_from_directory(_DASHBOARD_DIR, 'index.html')


@dashboard_bp.route('/favicon.svg')
def favicon():
    return send_from_directory(_DASHBOARD_DIR, 'favicon.svg')


@dashboard_bp.route('/favicon.ico')
def favicon_ico():
    return send_from_directory(_DASHBOARD_DIR, 'favicon.ico')
