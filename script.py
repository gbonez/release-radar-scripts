import os
import json
import datetime
import time
import requests
from spotipy import Spotify
from spotipy.oauth2 import SpotifyOAuth
from spotipy.exceptions import SpotifyException

# ==== CONFIGURATION ====
print(f"Starting Release Radar Script for {datetime.datetime.now().strftime('%m/%d/%y')}...")
time.sleep(1)

SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI = os.environ.get("BASE_URL") + "/callback" if os.environ.get("BASE_URL") else None
SPOTIFY_REFRESH_TOKEN = os.environ.get("SPOTIFY_REFRESH_TOKEN")

MY_PHONE = os.environ.get("MY_PHONE_NUMBER")
SELFPING_API_KEY = os.environ.get("SELFPING_API_KEY")
SELFPING_ENDPOINT = "https://www.selfping.com/api/sms"

LASTFM_API_KEY = os.environ.get("LASTFM_API_KEY")
LASTFM_USERNAME = os.environ.get("LASTFM_USERNAME")

ARTISTS_FILE = "artists.json"
RELEASES_FILE = "releases.json"

PLAYLIST_NAME = "Enhanced Releases"
PLAYLIST_ID = os.environ.get("PLAYLIST_ID")

scope = "user-library-read playlist-modify-private playlist-modify-public user-top-read"

# ==== SPOTIFY AUTH ====
auth_manager = SpotifyOAuth(
    client_id=SPOTIFY_CLIENT_ID,
    client_secret=SPOTIFY_CLIENT_SECRET,
    redirect_uri=SPOTIFY_REDIRECT_URI,
    scope=scope,
    cache_path=None
)
if SPOTIFY_REFRESH_TOKEN:
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
            try:
                status = getattr(e, "http_status", None)
                headers = getattr(e, "headers", {})
            except Exception:
                status = None
                headers = {}
            if status == 429:
                retry_after = int(headers.get("Retry-After", 5))
                print(f"Rate limited by Spotify, retrying after {retry_after} seconds...")
                time.sleep(retry_after)
            else:
                raise
        except Exception as e:
            raise

# ==== LAST.FM PLAY COUNTS ====
def fetch_lastfm_play_counts(artist_name):
    if not (LASTFM_API_KEY and LASTFM_USERNAME):
        return 0
    url = "http://ws.audioscrobbler.com/2.0/"
    params = {
        "method": "artist.getinfo",
        "artist": artist_name,
        "user": LASTFM_USERNAME,
        "api_key": LASTFM_API_KEY,
        "format": "json"
    }
    try:
        response = requests.get(url, params=params, timeout=8)
        data = response.json()
        return int(data.get("artist", {}).get("stats", {}).get("userplaycount", 0) or 0)
    except Exception as e:
        print(f"Failed to fetch Last.fm data for {artist_name}: {e}")
        return 0

# ==== ARTISTS ====
def update_artists_file():
    """
    Scans the entire saved tracks library (paginated) and
    builds/updates artists.json with liked_count and recent plays.
    This ensures ALL artists from liked songs are present and counted.
    """
    if os.path.exists(ARTISTS_FILE):
        with open(ARTISTS_FILE, "r") as f:
            data = json.load(f)
        if "artists" not in data:
            data = {"artists": {}}
    else:
        data = {"artists": {}}

    print("Scanning entire saved library to update artists and liked counts...")
    offset = 0
    limit = 50
    total_tracks_seen = 0
    liked_counts = {}

    while True:
        results = safe_spotify_call(sp.current_user_saved_tracks, limit=limit, offset=offset)
        items = results.get("items", [])
        if not items:
            break
        for item in items:
            total_tracks_seen += 1
            track = item.get("track")
            if not track:
                continue
            for artist in track.get("artists", []):
                artist_id = artist.get("id")
                artist_name = artist.get("name")
                if not artist_id:
                    continue
                liked_counts.setdefault(artist_id, {"name": artist_name, "count": 0})
                liked_counts[artist_id]["count"] += 1
        if len(items) < limit:
            break
        offset += limit

    total_added = 0
    for artist_id, info in liked_counts.items():
        if artist_id in data["artists"]:
            data["artists"][artist_id]["name"] = info["name"]
            data["artists"][artist_id]["liked_count"] = info["count"]
        else:
            recent_plays = fetch_lastfm_play_counts(info["name"])
            data["artists"][artist_id] = {
                "name": info["name"],
                "liked_count": info["count"],
                "recent_artist_plays": recent_plays
            }
            total_added += 1

    removed = []
    existing_ids = set(liked_counts.keys())
    for aid in list(data["artists"].keys()):
        if aid not in existing_ids:
            removed.append(aid)
            data["artists"].pop(aid, None)

    with open(ARTISTS_FILE, "w") as f:
        json.dump(data, f, indent=2)

    print(f"Scanned {total_tracks_seen} saved tracks. New artists added: {total_added}. Removed missing artists: {len(removed)}")
    return data

# ==== RECENT LISTENING SCORES ====
def fetch_recent_listening_scores(time_range='medium_term', top_limit=50):
    recent_scores = {}
    results = safe_spotify_call(sp.current_user_top_artists, limit=top_limit, time_range=time_range)
    items = results.get('items', [])
    max_rank = max(0, len(items) - 1)
    for rank, artist in enumerate(items):
        recent_scores[artist['id']] = max_rank - rank
    return recent_scores

# ==== CHECK NEW RELEASES ====
def check_new_releases(artists_data, recent_scores):
    releases = []
    seven_days_ago = datetime.datetime.now() - datetime.timedelta(days=7)
    for artist_id, artist_info in artists_data["artists"].items():
        if artist_info.get("liked_count", 0) <= 1:
            continue
        artist_name = artist_info["name"]
        recent_plays = artist_info.get("recent_artist_plays", 0)
        print(f"Checking releases for {artist_name}...")
        albums = safe_spotify_call(sp.artist_albums, artist_id, album_type="album,single", limit=10)
        for album in albums.get("items", []):
            track_count = album.get("total_tracks", 0)
            if track_count == 0:
                continue
            release_dt = parse_release_date(album.get("release_date", ""))
            if not release_dt or release_dt < seven_days_ago:
                continue
            if track_count <= 3:
                r_type = "single"
            elif track_count <= 6:
                r_type = "ep"
            else:
                r_type = "album"
            releases.append({
                "name": album.get("name"),
                "artist": artist_name,
                "artist_id": artist_id,
                "type": r_type,
                "album_id": album.get("id"),
                "liked_count": artist_info.get("liked_count", 0),
                "recent_score": recent_scores.get(artist_id, 0),
                "recent_artist_plays": recent_plays,
                "release_date": album.get("release_date")
            })
    releases_sorted = sorted(
        releases,
        key=lambda r: (r["liked_count"] + r["recent_score"] + r["recent_artist_plays"]),
        reverse=True
    )
    with open(RELEASES_FILE, "w") as f:
        json.dump(releases_sorted, f, indent=2)
    print(f"✅ Found {len(releases_sorted)} new releases in the past 7 days.")
    return releases_sorted

# ==== PLAYLIST MANAGEMENT ====
def get_or_create_playlist():
    user_id = safe_spotify_call(sp.current_user)["id"]
    if PLAYLIST_ID:
        try:
            return safe_spotify_call(sp.playlist, PLAYLIST_ID)
        except Exception:
            print("Hardcoded playlist ID invalid or inaccessible. Creating new playlist...")
    playlists = safe_spotify_call(sp.current_user_playlists, limit=50)
    for pl in playlists.get("items", []):
        if pl.get("name") == PLAYLIST_NAME:
            return pl
    playlist = safe_spotify_call(sp.user_playlist_create, user_id, name=PLAYLIST_NAME, public=False)
    print(f"Created new playlist '{PLAYLIST_NAME}'")
    return playlist

def add_new_releases_to_playlist(releases, playlist):
    artist_existing_track = {}
    existing_track_ids = set()
    # Store existing releases by artist with their album names for duplicate detection
    artist_existing_releases = {}
    
    offset = 0
    while True:
        result = safe_spotify_call(sp.playlist_tracks, playlist["id"], limit=100, offset=offset)
        items = result.get("items", [])
        for item in items:
            track = item.get("track")
            if not track:
                continue
            track_id = track.get("id")
            if track_id:
                existing_track_ids.add(track_id)
            
            # Store album information for duplicate checking
            album = track.get("album", {})
            album_name = album.get("name", "").lower().strip()
            
            artist_ids = [a["id"] for a in track.get("artists", []) if a.get("id")]
            for aid in artist_ids:
                if aid not in artist_existing_track and track_id:
                    artist_existing_track[aid] = track_id
                # Store existing releases by artist
                if aid not in artist_existing_releases:
                    artist_existing_releases[aid] = []
                artist_existing_releases[aid].append({
                    "album_name": album_name,
                    "track_id": track_id,
                    "track_name": track.get("name", "").lower().strip()
                })
        if len(items) < 100:
            break
        offset += 100

    added_releases = []
    # Track which artists we've already added to avoid multiple notifications per artist
    notified_artists = set()

    for r in releases:
        album_id = r.get("album_id")
        if not album_id:
            continue
        album_tracks = safe_spotify_call(sp.album_tracks, album_id)
        album_items = album_tracks.get("items", [])
        if not album_items:
            continue
        first_track_id = album_items[0].get("id")
        if not first_track_id:
            continue

        # Check if exact track is already in playlist
        if first_track_id in existing_track_ids:
            print(f"Skipping '{r['name']}' by {r['artist']} — exact track already in playlist.")
            continue

        # Check for duplicate track/release by same artist
        current_album_name = r.get("name", "").lower().strip()
        current_track_name = album_items[0].get("name", "").lower().strip()
        artist_id = r.get("artist_id")
        
        is_duplicate = False
        if artist_id in artist_existing_releases:
            for existing_release in artist_existing_releases[artist_id]:
                existing_track_name = existing_release["track_name"]
                existing_album_name = existing_release["album_name"]
                
                # Check if track name is exactly the same
                if current_track_name == existing_track_name:
                    # If track names match, check if albums are also the same
                    if current_album_name == existing_album_name:
                        print(f"Skipping '{r['name']}' by {r['artist']} — same track '{album_items[0].get('name')}' from same album already in playlist.")
                        is_duplicate = True
                        break
                    else:
                        print(f"Skipping '{r['name']}' by {r['artist']} — same track '{album_items[0].get('name')}' already in playlist from different album.")
                        is_duplicate = True
                        break
        
        if is_duplicate:
            continue

        existing_track_id_by_artist = artist_existing_track.get(r["artist_id"])

        # Check if this artist already existed in the playlist before this run
        artist_existed_in_playlist = existing_track_id_by_artist is not None
        
        if existing_track_id_by_artist and existing_track_id_by_artist != first_track_id:
            safe_spotify_call(sp.playlist_remove_all_occurrences_of_items, playlist["id"], [existing_track_id_by_artist])
            print(f"🧹 Removed old release by {r['artist']} (track {existing_track_id_by_artist})")

            if existing_track_id_by_artist in existing_track_ids:
                existing_track_ids.discard(existing_track_id_by_artist)

        if first_track_id in existing_track_ids:
            print(f"After cleanup, track {first_track_id} still in playlist — skipping.")
            continue

        safe_spotify_call(sp.playlist_add_items, playlist["id"], [first_track_id])
        
        # Only add to notification list if:
        # 1. Artist didn't exist in playlist before this run, OR
        # 2. Artist existed but we haven't already notified about them in this run
        if not artist_existed_in_playlist or r["artist_id"] not in notified_artists:
            added_releases.append(r)
            notified_artists.add(r["artist_id"])
            print(f"🎧 Added new release '{r['name']}' by {r['artist']} (will notify)")
        else:
            print(f"🎧 Added new release '{r['name']}' by {r['artist']} (no notification - already notified about this artist)")

        # Update tracking structures
        existing_track_ids.add(first_track_id)
        artist_existing_track[r["artist_id"]] = first_track_id
        
        # Add to existing releases tracking
        if artist_id not in artist_existing_releases:
            artist_existing_releases[artist_id] = []
        artist_existing_releases[artist_id].append({
            "album_name": current_album_name,
            "track_id": first_track_id,
            "track_name": album_items[0].get("name", "").lower().strip()
        })

    return added_releases

def remove_old_tracks_from_playlist(playlist, days=10):
    now = datetime.datetime.utcnow()
    offset = 0
    removed_count = 0
    while True:
        result = safe_spotify_call(sp.playlist_tracks, playlist["id"], limit=100, offset=offset)
        items = result.get("items", [])
        if not items:
            break
        tracks_to_remove = []
        for item in items:
            added_at = item.get("added_at")
            if not added_at:
                continue
            added_dt = datetime.datetime.strptime(added_at, "%Y-%m-%dT%H:%M:%SZ")
            if (now - added_dt).days > days:
                uri = item.get("track", {}).get("uri")
                if uri:
                    tracks_to_remove.append(uri)
        if tracks_to_remove:
            safe_spotify_call(sp.playlist_remove_all_occurrences_of_items, playlist["id"], tracks_to_remove)
            removed_count += len(tracks_to_remove)
        if len(items) < 100:
            break
        offset += 100
    if removed_count:
        print(f"🧹 Removed {removed_count} old tracks from playlist.")
    else:
        print("No old tracks to remove.")

# ==== SMS via SelfPing ====
def send_sms(new_releases, playlist_url):
    if not new_releases:
        print("No new releases to notify.")
        return
    
    # Filter out artists with very low liked counts (should already be filtered, but double-check)
    filtered_releases = [r for r in new_releases if r.get("liked_count", 0) > 1]
    
    # Sort by multiple criteria for better ranking:
    # 1. Primary: liked_count (descending)
    # 2. Secondary: recent_score + recent_artist_plays (descending)
    ranked_releases = sorted(
        filtered_releases,
        key=lambda r: (
            r.get("liked_count", 0),
            r.get("recent_score", 0) + r.get("recent_artist_plays", 0)
        ),
        reverse=True
    )
    
    today = datetime.datetime.now().strftime("%m/%d/%y")
    message_body = f"🎵 New releases for {today}:\n\n"
    
    # Show top 5 releases with liked count for context
    for i, r in enumerate(ranked_releases[:5], start=1):
        message_body += f"{i}. '{r['name']}' by {r['artist']} ({r['type']})\n"
    
    if len(ranked_releases) > 5:
        message_body += f"\n+ {len(ranked_releases) - 5} more releases in playlist"
    
    message_body += f"\nFull playlist: {playlist_url}"
    
    print(f"📱 Sending notification for {len(ranked_releases[:5])} top releases (ranked by likes)")
    
    data = {"to": MY_PHONE, "message": message_body}
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {SELFPING_API_KEY}"}
    try:
        response = requests.post(SELFPING_ENDPOINT, headers=headers, json=data, timeout=8)
        if response.status_code == 200:
            print("📱 SMS notification sent via SelfPing!")
        else:
            print(f"⚠️ Failed to send SMS. Status code: {response.status_code}, Response: {response.text}")
    except Exception as e:
        print(f"⚠️ Exception sending SMS: {e}")

# ==== MAIN ====
if __name__ == "__main__":
    artists_data = update_artists_file()
    recent_scores = fetch_recent_listening_scores()
    releases = check_new_releases(artists_data, recent_scores)

    playlist = get_or_create_playlist()
    new_releases_added = add_new_releases_to_playlist(releases, playlist)
    remove_old_tracks_from_playlist(playlist, days=10)

    send_sms(new_releases_added, playlist.get("external_urls", {}).get("spotify", ""))
