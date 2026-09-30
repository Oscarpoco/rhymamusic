import os
import re
import time
import uuid
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import urlparse

import click
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin, login_user, LoginManager, login_required, logout_user, current_user
from sqlalchemy import inspect, text
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

app = Flask(__name__)

app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'your_secret_bronze_key')
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['MAX_CONTENT_LENGTH'] = 8 * 1024 * 1024  # 8 MB upload limit

# Database Configuration
uri = os.environ.get('DATABASE_URL') or os.environ.get('POSTGRES_URL')
if uri:
    if uri.startswith("postgres://"):
        uri = uri.replace("postgres://", "postgresql://", 1)
else:
    # On Vercel, the root is read-only. We move the SQLite DB to /tmp/ if no DB is provided.
    if os.environ.get('VERCEL'):
        uri = 'sqlite:////tmp/site.db'
    else:
        uri = 'sqlite:///site.db'

app.config['SQLALCHEMY_DATABASE_URI'] = uri
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'pool_pre_ping': True}

# Configure where to save images
UPLOAD_FOLDER = os.environ.get('UPLOAD_FOLDER', 'static/uploads')
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

try:
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
except OSError:
    pass  # Read-only file systems (like Vercel)

HTTP_TIMEOUT = 10
DEFAULT_YOUTUBE_ID = 'prZ-ErkCkNw'
YOUTUBE_HANDLE = 'rhymangn'
# Minimum seconds between automatic YouTube lookups when no video is stored yet,
# so a failing lookup doesn't slow down every page view.
YOUTUBE_RETRY_SECONDS = 600
_last_youtube_attempt = 0.0

db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access the dashboard.'
login_manager.login_message_category = 'error'


# Models
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(20), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)


class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    price = db.Column(db.Float, nullable=False)
    image_url = db.Column(db.String(200), nullable=True)


class Track(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(100), nullable=False)
    audio_url = db.Column(db.String(200))  # Link to mp3 / streaming page
    is_new_release = db.Column(db.Boolean, default=False)


class Announcement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    content = db.Column(db.Text, nullable=False)
    date_posted = db.Column(db.DateTime, default=datetime.utcnow)


class Settings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    artist_id = db.Column(db.String(100), nullable=True)
    youtube_channel_id = db.Column(db.String(100), nullable=True)
    latest_youtube_id = db.Column(db.String(100), nullable=True)
    latest_youtube_title = db.Column(db.String(200), nullable=True)


@login_manager.user_loader
def load_user(user_id):
    try:
        return db.session.get(User, int(user_id))
    except (TypeError, ValueError):
        return None


@app.context_processor
def inject_now():
    return {'now': datetime.utcnow()}


# Helpers
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def get_settings(create=False):
    settings = Settings.query.first()
    if settings is None and create:
        settings = Settings()
        db.session.add(settings)
    return settings


def is_password_hash(value):
    return bool(value) and value.startswith(('scrypt:', 'pbkdf2:'))


def verify_password(user, candidate):
    if not user or not user.password or candidate is None:
        return False
    if is_password_hash(user.password):
        return check_password_hash(user.password, candidate)
    # Legacy accounts stored the password in plain text.
    return user.password == candidate


def upgrade_legacy_password(user, candidate):
    if is_password_hash(user.password):
        return
    try:
        user.password = generate_password_hash(candidate)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        app.logger.warning(f"Could not upgrade password hash for '{user.username}': {e}")


def is_safe_redirect(target):
    if not target or not target.startswith('/') or target.startswith('//'):
        return False
    parsed = urlparse(target)
    return not parsed.scheme and not parsed.netloc


def extract_youtube_id(value):
    value = (value or '').strip()
    if not value:
        return None
    patterns = [
        r'[?&]v=([\w-]{6,})',
        r'youtu\.be/([\w-]{6,})',
        r'youtube\.com/(?:embed|shorts|live)/([\w-]{6,})',
    ]
    for pattern in patterns:
        match = re.search(pattern, value)
        if match:
            return match.group(1)
    return value


def fetch_url(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as response:
        return response.read()


def ensure_latest_video():
    """Return settings, fetching the latest YouTube video if none is stored yet (throttled)."""
    global _last_youtube_attempt
    settings = Settings.query.first()
    if settings and settings.latest_youtube_id:
        return settings
    if time.time() - _last_youtube_attempt < YOUTUBE_RETRY_SECONDS:
        return settings
    _last_youtube_attempt = time.time()
    success, msg = sync_youtube_video()
    if not success:
        app.logger.warning(f"YouTube auto-sync failed: {msg}")
    return Settings.query.first()


def wants_json():
    return request.path.startswith('/api/')


# Public routes
@app.route('/')
def home():
    latest_single = None
    try:
        latest_single = Track.query.filter_by(is_new_release=True).first()
    except Exception as e:
        db.session.rollback()
        app.logger.warning(f"Error querying latest single: {e}")

    latest_video = None
    try:
        settings = ensure_latest_video()
        if settings and settings.latest_youtube_id:
            latest_video = {
                'youtube_id': settings.latest_youtube_id,
                'title': settings.latest_youtube_title or 'New Release Video'
            }
    except Exception as e:
        db.session.rollback()
        app.logger.warning(f"Error fetching YouTube settings: {e}")

    return render_template('home.html', title="Home", latest_single=latest_single,
                           latest_video=latest_video, default_youtube_id=DEFAULT_YOUTUBE_ID)


@app.route('/music')
def music():
    return render_template('music.html', title="Music")


GALLERY_ITEMS = [
    'Rhyma Studio Shoot',
    'Writing Session',
    'Creative Process',
    'Lounge Session',
    'Luxury Portrait',
    'Afro Noir Shoot',
    'Cover Shoot',
    'Khaki Vest Pose',
    'Archway Studio Shoot',
    'Focus Session',
    'Desk Creative Session',
    'Studio Wheel Pose',
]


def get_gallery_data():
    return [
        {
            'id': i,
            'url': url_for('static', filename=f'images/gallery/rhyma_gallery_{i}.webp'),
            'thumb': url_for('static', filename=f'images/gallery/thumbs/rhyma_gallery_{i}.webp'),
            'title': title,
        }
        for i, title in enumerate(GALLERY_ITEMS, start=1)
    ]


@app.route('/gallery')
def gallery():
    return render_template('gallery.html', title="Gallery", images=get_gallery_data())


@app.route('/merch')
def merch():
    products = []
    try:
        products = Product.query.order_by(Product.id.desc()).all()
    except Exception as e:
        db.session.rollback()
        app.logger.warning(f"Error loading products: {e}")
    return render_template('merch.html', title="Merch", products=products)


@app.route('/about')
def about():
    return render_template('about.html', title="About")


@app.route('/bookings', methods=['GET', 'POST'])
def bookings():
    return render_template('bookings.html', title="Bookings")


@app.route('/contact', methods=['GET', 'POST'])
def contact():
    return render_template('contact.html', title="Contact")


@app.route('/privacy')
def privacy():
    return render_template('privacy.html', title="Privacy Policy")


@app.route('/terms')
def terms():
    return render_template('terms.html', title="Terms of Service")


# Auth
@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('admin'))

    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        user = User.query.filter_by(username=username).first() if username else None
        if verify_password(user, password):
            upgrade_legacy_password(user, password)
            login_user(user)
            next_url = request.args.get('next')
            return redirect(next_url if is_safe_redirect(next_url) else url_for('admin'))
        flash('Invalid username or password.', 'error')
        return render_template('login.html', username=username), 401

    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out.', 'success')
    return redirect(url_for('home'))


# Admin
@app.route('/admin', methods=['GET', 'POST'])
@login_required
def admin():
    products = Product.query.order_by(Product.id.desc()).all()
    tracks = Track.query.order_by(Track.id.desc()).all()
    settings = Settings.query.first()
    return render_template('admin.html', products=products, tracks=tracks, settings=settings)


@app.route('/admin/add-track', methods=['POST'])
@login_required
def add_track():
    title = (request.form.get('title') or '').strip()
    url = (request.form.get('url') or '').strip()
    if not title:
        flash('Please enter a track name.', 'error')
        return redirect(url_for('admin') + '#music')
    db.session.add(Track(title=title[:100], audio_url=url[:200] or None))
    db.session.commit()
    flash(f"Track '{title}' saved.", 'success')
    return redirect(url_for('admin') + '#music')


@app.route('/admin/add-merch', methods=['POST'])
@login_required
def add_merch():
    name = (request.form.get('name') or '').strip()
    try:
        price = round(float(request.form.get('price', '')), 2)
    except (TypeError, ValueError):
        price = None

    if not name or price is None or price < 0:
        flash('Please enter a product name and a valid price.', 'error')
        return redirect(url_for('admin') + '#merch')

    image_path = None
    file = request.files.get('image')
    if file and file.filename:
        if not allowed_file(file.filename):
            flash('Image must be a PNG, JPG, GIF or WEBP file.', 'error')
            return redirect(url_for('admin') + '#merch')
        filename = f"{uuid.uuid4().hex[:8]}_{secure_filename(file.filename)}"
        try:
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
            image_path = f'uploads/{filename}'
        except OSError as e:
            app.logger.warning(f"Could not save upload: {e}")
            flash('The image could not be stored on this server, so the product was saved without it.', 'error')

    db.session.add(Product(name=name[:100], price=price, image_url=image_path))
    db.session.commit()
    flash(f"'{name}' added to merch.", 'success')
    return redirect(url_for('admin') + '#merch')


@app.route('/delete-merch/<int:id>', methods=['POST'])
@login_required
def delete_merch(id):
    item = Product.query.get_or_404(id)
    name, image_url = item.name, item.image_url
    db.session.delete(item)
    db.session.commit()

    if image_url and image_url.startswith('uploads/'):
        try:
            os.remove(os.path.join(app.config['UPLOAD_FOLDER'], os.path.basename(image_url)))
        except OSError:
            pass

    flash(f"'{name}' removed.", 'success')
    return redirect(url_for('admin') + '#merch')


# --- YouTube Integration ---
def get_channel_id_from_handle(handle=YOUTUBE_HANDLE):
    clean_handle = handle if handle.startswith('@') else '@' + handle
    try:
        html = fetch_url(f"https://www.youtube.com/{clean_handle}").decode('utf-8', errors='ignore')
        for pattern in (r'youtube\.com/channel/(UC[\w-]+)', r'channel_id=([\w-]+)', r'"channelId":"(UC[\w-]+)"'):
            match = re.search(pattern, html)
            if match:
                return match.group(1)
    except Exception as e:
        app.logger.warning(f"Error fetching channel page: {e}")
    return None


def sync_youtube_video():
    try:
        settings = get_settings(create=True)
        db.session.commit()

        channel_id = settings.youtube_channel_id or os.environ.get('YOUTUBE_CHANNEL_ID')
        if not channel_id:
            channel_id = get_channel_id_from_handle(YOUTUBE_HANDLE)
            if channel_id:
                settings.youtube_channel_id = channel_id
                db.session.commit()

        if not channel_id:
            return False, "Could not find YouTube Channel ID."

        xml_data = fetch_url(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}")
        root = ET.fromstring(xml_data)
        ns = {
            'atom': 'http://www.w3.org/2005/Atom',
            'yt': 'http://www.youtube.com/xml/schemas/2015'
        }
        entry = root.find('atom:entry', ns)
        if entry is not None:
            video_id_elem = entry.find('yt:videoId', ns)
            title_elem = entry.find('atom:title', ns)
            video_id = video_id_elem.text if video_id_elem is not None else None
            title = title_elem.text if title_elem is not None else None

            if video_id:
                settings.latest_youtube_id = video_id
                settings.latest_youtube_title = (title or '')[:200] or None
                db.session.commit()
                return True, f"Synced YouTube video: '{title}' ({video_id})"
        return False, "No video entries found in YouTube RSS feed."
    except Exception as e:
        db.session.rollback()
        return False, str(e)


# --- Spotify Integration ---
def sync_spotify_music():
    try:
        settings = Settings.query.first()
        artist_id = settings.artist_id if (settings and settings.artist_id) else os.environ.get('SPOTIFY_ARTIST_ID')
        if not artist_id:
            return False, "Artist ID not configured in Settings or Env (SPOTIFY_ARTIST_ID)."

        sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(), requests_timeout=HTTP_TIMEOUT)
        # Fetch several so they can be sorted by release date.
        results = sp.artist_albums(artist_id, album_type='single,album', limit=5)
        items = results.get('items') or []
        if not items:
            return False, "No releases found for this Artist ID on Spotify."

        latest_album = sorted(items, key=lambda x: x.get('release_date', ''), reverse=True)[0]
        track_title = latest_album['name'][:100]
        audio_url = latest_album['external_urls']['spotify']
        release_date = latest_album.get('release_date', 'unknown date')

        # Only clear the current flag once a replacement has been fetched successfully.
        for t in Track.query.filter_by(is_new_release=True).all():
            t.is_new_release = False

        added_count = 0
        existing = Track.query.filter_by(title=track_title).first()
        if existing:
            existing.is_new_release = True
            existing.audio_url = audio_url
        else:
            db.session.add(Track(title=track_title, audio_url=audio_url, is_new_release=True))
            added_count += 1
        db.session.commit()
        return True, f"Latest release: '{track_title}' ({release_date}). Added {added_count} new track(s)."
    except Exception as e:
        db.session.rollback()
        return False, str(e)


@app.route('/admin/sync-spotify', methods=['POST'])
@login_required
def sync_spotify_route():
    success, msg = sync_spotify_music()
    app.logger.info(f"Spotify Sync: {success} - {msg}")
    flash(f"Spotify: {msg}", 'success' if success else 'error')
    return redirect(url_for('admin') + '#music')


@app.route('/admin/sync-youtube', methods=['POST'])
@login_required
def sync_youtube_route():
    success, msg = sync_youtube_video()
    app.logger.info(f"YouTube Sync: {success} - {msg}")
    flash(f"YouTube: {msg}", 'success' if success else 'error')
    return redirect(url_for('admin') + '#music')


@app.route('/admin/settings', methods=['POST'])
@login_required
def update_settings():
    aid = (request.form.get('artist_id') or '').strip()
    settings = get_settings(create=True)
    settings.artist_id = aid or None
    db.session.commit()
    flash('Spotify settings saved.', 'success')
    return redirect(url_for('admin') + '#music')


@app.route('/admin/youtube-settings', methods=['POST'])
@login_required
def update_youtube_settings():
    cid = (request.form.get('youtube_channel_id') or '').strip()
    vid = extract_youtube_id(request.form.get('latest_youtube_id'))
    settings = get_settings(create=True)
    if cid:
        settings.youtube_channel_id = cid
    if vid:
        if vid != settings.latest_youtube_id:
            settings.latest_youtube_title = None
        settings.latest_youtube_id = vid
    db.session.commit()
    flash('YouTube settings saved.', 'success')
    return redirect(url_for('admin') + '#music')


# --- API Endpoints ---
@app.route('/api/products', methods=['GET'])
def get_products():
    products = Product.query.all()
    data = [{'id': p.id, 'name': p.name, 'price': p.price, 'image_url': p.image_url} for p in products]
    return jsonify(data)


@app.route('/api/tracks', methods=['GET'])
def get_tracks():
    tracks = Track.query.all()
    data = [{'id': t.id, 'title': t.title, 'audio_url': t.audio_url, 'is_new_release': t.is_new_release} for t in tracks]
    return jsonify(data)


@app.route('/api/latest-video', methods=['GET'])
def get_latest_video():
    settings = ensure_latest_video()
    youtube_id = settings.latest_youtube_id if settings and settings.latest_youtube_id else DEFAULT_YOUTUBE_ID
    title = settings.latest_youtube_title if settings and settings.latest_youtube_title else 'New Release Video'
    return jsonify({
        'youtube_id': youtube_id,
        'title': title,
        'embed_url': f"https://www.youtube.com/embed/{youtube_id}"
    })


@app.route('/api/gallery', methods=['GET'])
def get_gallery():
    return jsonify(get_gallery_data())


@app.route('/api/cron/sync', methods=['GET'])
def cron_sync():
    # Vercel sends: Authorization: Bearer <CRON_SECRET>
    cron_secret = os.environ.get('CRON_SECRET')
    if cron_secret and request.headers.get('Authorization') != f"Bearer {cron_secret}":
        return jsonify({'error': 'Unauthorized'}), 401

    spotify_success, spotify_msg = sync_spotify_music()
    youtube_success, youtube_msg = sync_youtube_video()
    return jsonify({
        'spotify': {'success': spotify_success, 'message': spotify_msg},
        'youtube': {'success': youtube_success, 'message': youtube_msg}
    })


# Error pages
@app.errorhandler(404)
def not_found(e):
    if wants_json():
        return jsonify({'error': 'Not found'}), 404
    return render_template('error.html', title="Page Not Found", code=404,
                           message="The page you're looking for doesn't exist or has moved."), 404


@app.errorhandler(413)
def too_large(e):
    if current_user.is_authenticated:
        flash('That file is too large. The maximum upload size is 8 MB.', 'error')
        return redirect(url_for('admin') + '#merch')
    return render_template('error.html', title="File Too Large", code=413,
                           message="The uploaded file is too large."), 413


@app.errorhandler(500)
def server_error(e):
    db.session.rollback()
    if wants_json():
        return jsonify({'error': 'Internal server error'}), 500
    return render_template('error.html', title="Something Went Wrong", code=500,
                           message="An unexpected error occurred. Please try again in a moment."), 500


# CLI: flask --app app create-admin <username>
@app.cli.command('create-admin')
@click.argument('username')
@click.password_option()
def create_admin(username, password):
    """Create an admin user, or reset the password of an existing one."""
    user = User.query.filter_by(username=username).first()
    if user:
        user.password = generate_password_hash(password)
        click.echo(f"Password updated for '{username}'.")
    else:
        db.session.add(User(username=username, password=generate_password_hash(password)))
        click.echo(f"Admin '{username}' created.")
    db.session.commit()


# Database initialization for Vercel/Production
def init_db():
    try:
        with app.app_context():
            db.create_all()
            inspector = inspect(db.engine)

            if inspector.has_table('settings'):
                existing = {col['name'] for col in inspector.get_columns('settings')}
                missing = [
                    (name, ddl) for name, ddl in (
                        ('youtube_channel_id', 'VARCHAR(100)'),
                        ('latest_youtube_id', 'VARCHAR(100)'),
                        ('latest_youtube_title', 'VARCHAR(200)'),
                    ) if name not in existing
                ]
                if missing:
                    with db.engine.begin() as conn:
                        for name, ddl in missing:
                            conn.execute(text(f"ALTER TABLE settings ADD COLUMN {name} {ddl}"))

            # Password hashes don't fit the original VARCHAR(60); SQLite doesn't enforce lengths.
            if db.engine.dialect.name == 'postgresql' and inspector.has_table('user'):
                for col in inspector.get_columns('user'):
                    length = getattr(col['type'], 'length', None)
                    if col['name'] == 'password' and length and length < 255:
                        with db.engine.begin() as conn:
                            conn.execute(text('ALTER TABLE "user" ALTER COLUMN password TYPE VARCHAR(255)'))
    except Exception as e:
        print(f"Database initialization skipped or failed: {e}")


init_db()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
