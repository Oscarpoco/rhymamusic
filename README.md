# Rhymamusic (Flask)

A high-end, minimalist rhymamusic website built with Python Flask.

## Features
-   **Server-Side Rendering (SSR)** for SEO optimization.
-   **Dynamic Music Page**: Top tracks, latest releases, and drop countdowns.
-   **Booking System**: Specialized inquiry form.
-   **Responsive Design**: Mobile-friendly navigation and layout.
-   **Admin Dashboard** (`/admin`): Merch, tracks, Spotify and YouTube sync.

## Project Structure
-   `app.py`: Main Flask application.
-   `templates/`: HTML files (Jinja2).
-   `static/`: CSS, JS, Images.

## How to Run

1.  **Install Dependencies**:
    ```bash
    pip install -r requirements.txt
    ```

2.  **Create an admin account** (also resets the password of an existing user):
    ```bash
    flask --app app create-admin <username>
    ```

3.  **Start the Server**:
    ```bash
    python app.py
    ```

4.  **Visit**: Open `http://localhost:5000`

## Environment Variables
All are optional locally; set them in production.

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | Signs login sessions. |
| `DATABASE_URL` / `POSTGRES_URL` | Database. Falls back to SQLite. |
| `SPOTIPY_CLIENT_ID`, `SPOTIPY_CLIENT_SECRET` | Spotify API credentials for release sync. |
| `SPOTIFY_ARTIST_ID` | Used if no artist ID is saved in the dashboard. |
| `YOUTUBE_CHANNEL_ID` | Skips looking up the channel from the `@rhymangn` handle. |
| `CRON_SECRET` | Protects `/api/cron/sync`. |
| `UPLOAD_FOLDER` | Where merch images are saved (default `static/uploads`). |

## Customization
-   **Styles**: Edit `static/css/styles.css` to change colors/fonts.
-   **Content**: Edit HTML files in `templates/`.
