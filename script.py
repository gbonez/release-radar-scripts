import os
import json
import datetime
import time
import requests
from spotipy import Spotify
from spotipy.oauth2 import SpotifyOAuth
from spotipy.exceptions import SpotifyException
from twilio.rest import Client

# Optional dry-run mode
DRY_RUN = os.environ.get("DRY_RUN") or 0

# ==== CONFIGURATION ====
SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI = (os.environ.get("BASE_URL") or "http://localhost:5000") + "/callback"
SPOTIFY_REFRESH_TOKEN = os.environ.get("SPOTIFY_REFRESH_TOKEN")

if not SPOTIFY_REFRESH_TOKEN:
    raise ValueError("❌ Missing SPOTIFY_REFRESH_TOKEN in environment variables.")

TWILIO_SID = os.environ.get("TWILIO_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_PHONE = os.environ.get("TWILIO_PHONE")
MY_PHONE = os.environ.get("MY_PHONE")

LASTFM_API_KEY = os.environ.get("LASTFM_API_KEY")
LASTFM_USERNAME = os.environ.get("LASTFM_USERNAME")

ARTISTS_FILE = "artists.json"
RELEASES_FILE = "releases.json"

PLAYLIST_NAME = "Enhanced Releases"
PLAYLIST_ID = os.environ.get("PLAYLIST_ID")  # optional hardcoded playlist ID

scope = "user-library-read playlist-modify-private playlist-modify-public user-top-read"

# ==== SPOTIFY AUTH ====
auth_manager = SpotifyOAuth(
    client_id=SPOTIFY_CLIENT_ID,
    client_secret=SPOTIFY_CLIENT_SECRET,
    redirect_uri=SPOTIFY_REDIRECT_URI,
    scope=scope,
    cache_path=None
)
auth_manager.refresh_access_token(SPOTIFY_REFRESH_TOKEN)
sp = Spotify(auth_manager=auth_manager)

# ==== HELPERS ====
def parse_release_date(date_str):
    try:
        if len(date_str) == 4:
            return datetime.datetime.strptime(date_str, "%Y")
        elif len(date_str) == 7:
            return datetime.datetime.strptime(date_str, "%Y-%m")
        else:
            return datetime.datetime.strptime(date_str, "%Y-%m-%d")
    except Exception:
        return None

def safe_spotify_call(func, *args, **kwargs):
    while True:
        try:
            return func(*args, **kwargs)
        except SpotifyException as e:
            if e.http_status == 429:
                retry_after = int(e.headers.get("Retry-After", 5))
                print(f"Rate limited by Spotify, retrying after {retry_after} seconds...")
                time.sleep(retry_after)
            else:
                raise

# ==== LAST.FM PLAY COUNTS ====
def fetch_lastfm_play_counts(artist_name):
    url = "http://ws.audioscrobbler.com/2.0/"
    params = {
        "method": "artist.getinfo",
        "artist": artist_name,
        "user": LASTFM_USERNAME,
        "api_key": LASTFM_API_KEY,
        "format": "json"
    }
    try:
        response = requests.get(url, params=params)
        data = response.json()
        playcount = int(data.get("artist", {}).get("stats", {}).get("userplaycount", 0))
        return playcount
    except Exception as e:
        print(f"Failed to fetch Last.fm data for {artist_name}: {e}")
        return 0

# ==== ARTISTS ====
def update_artists_file():
    if os.path.exists(ARTISTS_FILE):
        with open(ARTISTS_FILE, "r") as f:
            data = json.load(f)
        print("Existing artists.json found. Scanning 50 most recent liked songs for new artists...")
        results = safe_spotify_call(sp.current_user_saved_tracks, limit=50, offset=0)
        items = results["items"]
        total_added = 0
        for item in items:
            track = item["track"]
            for artist in track["artists"]:
                artist_id = artist["id"]
                artist_name = artist["name"]
                if artist_id in data["artists"]:
                    continue
                recent_plays = fetch_lastfm_play_counts(artist_name)
                print(f"Adding new artist {artist_name}")
                data["artists"][artist_id] = {
                    "name": artist_name,
                    "liked_count": 1,
                    "recent_artist_plays": recent_plays
                }
                total_added += 1
        print(f"Total new artists added from recent liked songs: {total_added}")
    else:
        print("No artists.json found. Scanning entire liked songs library...")
        data = {"artists": {}}
        offset = 0
        limit = 50
        total_added = 0
        while True:
            results = safe_spotify_call(sp.current_user_saved_tracks, limit=limit, offset=offset)
            items = results["items"]
            if not items:
                break
            for item in items:
                track = item["track"]
                for artist in track["artists"]:
                    artist_id = artist["id"]
                    artist_name = artist["name"]
                    if artist_id in data["artists"]:
                        data["artists"][artist_id]["liked_count"] += 1
                        continue
                    recent_plays = fetch_lastfm_play_counts(artist_name)
                    print(f"Adding artist {artist_name}")
                    data["artists"][artist_id] = {
                        "name": artist_name,
                        "liked_count": 1,
                        "recent_artist_plays": recent_plays
                    }
                    total_added += 1
            offset += limit
        print(f"Total artists added from full library: {total_added}")

    with open(ARTISTS_FILE, "w") as f:
        json.dump(data, f, indent=2)
    return data

# ==== RECENT LISTENING SCORES ====
def fetch_recent_listening_scores(time_range='medium_term', top_limit=50):
    recent_scores = {}
    results = safe_spotify_call(sp.current_user_top_artists, limit=top_limit, time_range=time_range)
    max_rank = top_limit - 1
    for rank, artist in enumerate(results['items']):
        recent_scores[artist['id']] = max_rank - rank
    return recent_scores

# ==== CHECK NEW RELEASES ====
def check_new_releases(artists_data, recent_scores):
    releases = []
    seven_days_ago = datetime.datetime.now() - datetime.timedelta(days=1)

    for artist_id, artist_info in artists_data["artists"].items():
        if artist_info["liked_count"] <= 2:
            continue
        artist_name = artist_info["name"]
        recent_plays = artist_info.get("recent_artist_plays", 0)
        print(f"Checking releases for {artist_name} (liked_count={artist_info['liked_count']}, recent artist plays={recent_plays})")

        albums = safe_spotify_call(sp.artist_albums, artist_id, album_type="album,single", limit=5)
        for album in albums["items"]:
            track_count = album.get("total_tracks", 0)
            if track_count == 0:
                continue

            release_dt = parse_release_date(album["release_date"])
            if not release_dt or release_dt < seven_days_ago:
                continue

            if track_count <= 3:
                r_type = "single"
            elif track_count <= 6:
                r_type = "ep"
            else:
                r_type = "album"

            print(f"Found new release: {album['name']}")
            releases.append({
                "name": album["name"],
                "artist": artist_name,
                "artist_id": artist_id,
                "type": r_type,
                "track_id": album["id"],
                "first_song": album["id"],
                "liked_count": artist_info["liked_count"],
                "recent_score": recent_scores.get(artist_id, 0),
                "recent_artist_plays": recent_plays
            })

    # Sort by combined score
    releases_sorted = sorted(
        releases,
        key=lambda r: (
            (r["liked_count"] / max(r["liked_count"], 1))
            + r["recent_score"]
            + r["recent_artist_plays"]
        ),
        reverse=True
    )

    with open(RELEASES_FILE, "w") as f:
        json.dump(releases_sorted, f, indent=2)

    print(f"Total new releases found: {len(releases_sorted)}")
    return releases_sorted

# ==== PLAYLIST MANAGEMENT ====
def get_or_create_playlist():
    """Return playlist object for 'Enhanced Releases'."""
    user_id = safe_spotify_call(sp.current_user)["id"]

    # Try hardcoded ID first
    if PLAYLIST_ID:
        try:
            playlist = safe_spotify_call(sp.playlist, PLAYLIST_ID)
            return playlist
        except SpotifyException:
            print("Hardcoded playlist ID not found, creating a new playlist...")

    # Search by name
    playlists = safe_spotify_call(sp.current_user_playlists, limit=50)
    for pl in playlists["items"]:
        if pl["name"] == PLAYLIST_NAME:
            return pl

    # Not found? Create new playlist
    playlist = safe_spotify_call(sp.user_playlist_create, user_id, name=PLAYLIST_NAME, public=False)
    print(f"Created playlist '{PLAYLIST_NAME}'")
    return playlist

def add_new_releases_to_playlist(releases, playlist):
    """Add only new releases to playlist. Returns added tracks info for SMS."""
    track_ids_in_playlist = []
    offset = 0
    while True:
        result = safe_spotify_call(sp.playlist_tracks, playlist["id"], limit=100, offset=offset)
        track_ids_in_playlist.extend([item["track"]["id"] for item in result["items"]])
        if len(result["items"]) < 100:
            break
        offset += 100

    added_releases = []
    track_ids_to_add = []
    for r in releases:
        album_tracks = safe_spotify_call(sp.album_tracks, r["track_id"])
        if not album_tracks["items"]:
            continue
        first_track_id = album_tracks["items"][0]["id"]
        if first_track_id not in track_ids_in_playlist:
            track_ids_to_add.append(first_track_id)
            added_releases.append(r)

    if track_ids_to_add:
        safe_spotify_call(sp.playlist_add_items, playlist["id"], track_ids_to_add)
        print(f"Added {len(track_ids_to_add)} new releases to '{playlist['name']}'")
    else:
        print("No new releases to add.")

    return added_releases

def remove_old_tracks_from_playlist(playlist, days=10):
    """Remove tracks older than `days` from playlist."""
    now = datetime.datetime.utcnow()
    offset = 0
    removed_count = 0

    while True:
        result = safe_spotify_call(sp.playlist_tracks, playlist["id"], limit=100, offset=offset)
        if not result["items"]:
            break

        tracks_to_remove = []
        for item in result["items"]:
            added_at = datetime.datetime.strptime(item["added_at"], "%Y-%m-%dT%H:%M:%SZ")
            if (now - added_at).days > days:
                tracks_to_remove.append(item["track"]["uri"])

        if tracks_to_remove:
            safe_spotify_call(sp.playlist_remove_all_occurrences_of_items, playlist["id"], tracks_to_remove)
            removed_count += len(tracks_to_remove)

        if len(result["items"]) < 100:
            break
        offset += 100

    if removed_count:
        print(f"Removed {removed_count} old tracks from '{playlist['name']}'")
    else:
        print("No old tracks to remove.")

# ==== SEND SMS ====
def send_sms(new_releases, playlist_url):
    if not new_releases:
        print("No new releases to notify via SMS.")
        return

    today = datetime.datetime.now().strftime("%m/%d/%y")
    message_body = f"Your new release list for {today} has been updated!\n\n"
    for i, r in enumerate(new_releases[:5], start=1):
        message_body += f"{i}. '{r['name']}' by {r['artist']} ({r['type']})\n"
    message_body += f"\nCheck all releases here: {playlist_url}"

    if DRY_RUN:
        print("DRY RUN: SMS content:")
        print(message_body)
        return

    client = Client(TWILIO_SID, TWILIO_AUTH_TOKEN)
    client.messages.create(body=message_body, from_=TWILIO_PHONE, to=MY_PHONE)
    print("SMS sent!")

# ==== MAIN ====
if __name__ == "__main__":
    artists_data = update_artists_file()
    recent_scores = fetch_recent_listening_scores(time_range='medium_term', top_limit=50)
    releases = check_new_releases(artists_data, recent_scores)

    playlist = get_or_create_playlist()
    new_releases_added = add_new_releases_to_playlist(releases, playlist)
    remove_old_tracks_from_playlist(playlist, days=10)

    send_sms(new_releases_added, playlist["external_urls"]["spotify"])