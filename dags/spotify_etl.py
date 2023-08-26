import os
import re
import pandas as pd
from dotenv import load_dotenv
import logging
from youtube_etl import load_to_bigquery

from google.cloud import bigquery

import spotipy
from spotipy.oauth2 import SpotifyOAuth

# from datetime import datetime
# from airflow import DAG
# from airflow.operators.python import PythonOperator
# from airflow.models import Variable


# default_args = {
#     'owner': 'spotify_client'
# }

load_dotenv()


def extract_playlists() -> pd.DataFrame:
    project_id = os.getenv('PROJECT_ID')
    client = bigquery.Client(project=project_id)

    sql = f"""
    SELECT
        youtube_playlist_id,
        playlist_name
    
    FROM `{project_id}.marts.youtube_playlists`
    ORDER BY playlist_name
    """

    df_playlists = client.query(sql).to_dataframe()
    return df_playlists


def extract_videos() -> pd.DataFrame:
    project_id = os.getenv('PROJECT_ID')
    threshold_ms = os.getenv('THRESHOLD_MS')
    client = bigquery.Client(project=project_id)

    sql = f"""
    SELECT
        v.video_id,
        p.playlist_name,
        v.channel_name,
        v.title,
        lower(v.description) description,
        v.duration_ms
    
    FROM `{project_id}.marts.youtube_videos` v

    LEFT JOIN `{project_id}.marts.youtube_playlists` p
    ON v.youtube_playlist_id = p.youtube_playlist_id

    ORDER BY p.playlist_name, v.channel_name, v.title, v.duration_ms
    """ # WHERE v.duration_ms < {threshold_ms}

    df_videos = client.query(sql).to_dataframe()
    return df_videos


def get_authorization_code():
    scope = ["user-library-modify", "playlist-modify-private"]
    sp = spotipy.Spotify(auth_manager=SpotifyOAuth(scope=scope))
    return sp


def get_user_id() -> str:
    user_info = sp.current_user()
    return user_info['id']


def create_spotify_playlists_from_df(row) -> str:
    """
    Create private, non-collaborative Spotify playlists from the dataframe.
    Save created playlist ids as a column in the original dataframe.
    """
    playlist = sp.user_playlist_create(user_id, row['playlist_name'], public=False, collaborative=False)
    return playlist['id']


def get_spotify_playlist_id(row) -> str:
    playlist = df_playlists[df_playlists['playlist_name'] == row['playlist_name']]
    if playlist.empty:
        logging.info(f'Spotify id not found for playlist "{row["playlist_name"]}", video "{row["title"]}" skipped.')
        return
    
    elif len(playlist) > 1:
        logging.info(f'{len(playlist)} spotify ids were found for playlist: "{row["playlist_name"]}", first id was chosen.')
    
    return playlist.iloc[0, 2]


def save_track(row, spotify_playlist_id: str) -> None:
    # title = re.sub('&', 'and', row['title'])

    # First try, depends on whether it is a Topic Channel
    if ' - Topic' in row['channel_name']:
        artist = re.sub(' - Topic', '', row['channel_name'])
        artist = re.sub('\'', ' ', artist)

        q = f'track:{row["title"]} artist:{artist}'
        found = search_track(row, spotify_playlist_id, q=q, search_type_id='0', limit=2)
    
    else:
        found = search_track(row, spotify_playlist_id, q=row['title'], search_type_id='1', limit=2)

    # Second try, track + space + track name in quotes
    if not found:
        q = f'track "{row["title"]}"'
        found = search_track(row, spotify_playlist_id, q=q, search_type_id='2', limit=2)

    # Third try, channel name + space + track title
    if not found:
        artist = re.sub(' - Topic', '', row['channel_name'])
        q = f'{artist} {row["title"]}'
        found = search_track(row, spotify_playlist_id, q=q, search_type_id='3', limit=2)

    if not found:
        print(f'Track "{row["title"]}" not found on Spotify')


def search_track(row, spotify_playlist_id: str, q: str, search_type_id: str, limit: int) -> bool:
    tracks = sp.search(q=q, limit=limit, type='track')

    for track_num, track in enumerate(tracks['tracks']['items']):
        artists, artists_in_title, track_in_title = [], 0, 0
        diff = abs(track['duration_ms'] - row['duration_ms'])

        for artist in track['artists']:
            artists.append(artist['name'])
            if artist['name'].lower() in row["title"].lower(): # case-insensitive match
                artists_in_title += 1
        
        if track['name'].lower() in row["title"].lower():
            track_in_title = 1

        # Difference in 5 seconds or both track name and at least one artist presented in video title
        if diff <= 5000 or (track_in_title and artists_in_title):
            print(f'Track "{row["title"]}" found on try: {track_num}, ' \
                  f'difference: {round(diff / 1000)} seconds. ')
            
            if (track['uri'], spotify_playlist_id) not in ((uri, playlist_id) for uri, playlist_id, *_ in spotify_log): # search with primary key
                status = 'saved'
                if spotify_playlist_id: # populate_playlists
                    # Add the track to the playlist
                    sp.playlist_add_items(spotify_playlist_id, [track['uri']])
                
                else: # populate_liked_songs
                    # Like the track
                    sp.current_user_saved_tracks_add([track['uri']])
            else:
                status = 'skipped'
                print(f'WARNING: Track "{row["title"]}" skipped (already exsist)')

            # if track['uri'] not in spotify_tracks: # condition doesn't matter
            spotify_tracks[track['uri']] = (track['album']['uri'],
                                            track['name'],
                                            '; '.join(artist for artist in artists),
                                            str(track['duration_ms']))
            
            # always make a note in log
            spotify_log.append((track['uri'],
                                spotify_playlist_id,
                                row['video_id'],
                                # 1, # category
                                str(track_num),
                                str(abs(diff)),
                                None,
                                q,
                                search_type_id,
                                status))

            return True
        
        else:

            return False


def save_album(row, spotify_playlist_id: str) -> None:
    
    # First try, just video title
    found = search_album(row, spotify_playlist_id, q=row["title"], search_type_id='1', limit=2)

    # Second try, album + space + album name in quotes
    if not found:
        q = f'album "{row["title"]}"'
        found = search_album(row, spotify_playlist_id, q=q, search_type_id='2', limit=2)

    if not found:
        print(f'Album "{row["title"]}" not found on Spotify')


def search_album(row, spotify_playlist_id: str, q: str, search_type_id: str, limit: int) -> bool:
    albums = sp.search(q=q, limit=limit, type='album')

    for album_num, album in enumerate(albums['albums']['items']):    
        tracks_uri: list[str] = [] # track uri
        tracks_info: list[tuple[str, str, int]] = [] # (track uri, track title, track duration ms)
        diff = row['duration_ms']
        tracks_in_desc = 0
        album_length = 0
        
        tracks = sp.album(album['uri'])
        for track in tracks['tracks']['items']:
            if track['name'].lower() in row['description']: # case-insensitive match
                tracks_in_desc += 1
            
            tracks_uri.append(track['uri'])
            tracks_info.append((track['uri'], track['name'], track['duration_ms']))
            
            album_length += track['duration_ms']
            diff -= track['duration_ms']
            if diff < -20000:
                break
        
        percent_in_desc = (tracks_in_desc / len(tracks_uri)) * 100 # in case a albums are same with a diffrence in few tracks
        
        # Difference in 40 seconds or 70%+ tracks in the YouTube video description (only if the total number of tracks is objective)
        if (abs(diff) < 40000) or (len(tracks_uri) >= 4 and percent_in_desc >= 70):
            print(f'Album "{row["title"]}" found on try {album_num}, '
                  f'difference: {round(diff / 1000)} seconds, '
                  f'{tracks_in_desc} of {len(tracks_uri)} track titles '
                  f'({round(percent_in_desc)}%) in the YouTube video description.')
            
            if (album['uri'], spotify_playlist_id) not in ((uri, playlist_id) for uri, playlist_id, *_ in spotify_log): # search with primary key
                status = 'saved'
                if spotify_playlist_id: # populate_playlists

                    # Add album tracks not present in the playlist to the playlist
                    sp.playlist_add_items(spotify_playlist_id, tracks_uri)

                    # Save the album to current user library
                    # sp.current_user_saved_albums_add([album['uri']])
                
                else: # populate_liked_songs

                    # Like all tracks in the album
                    # sp.current_user_saved_tracks_add(album_tracks_uri)

                    # Save the album to current user library
                    sp.current_user_saved_albums_add([album['uri']]) 
            else:
                status = 'skipped'
                print(f'WARNING: Album "{row["title"]}" skipped (already exsist)')

            # if album['uri'] not in spotify_albums:  # condition doesn't matter
            spotify_albums[album['uri']] = (album['name'],
                                            '; '.join(artist['name'] for artist in album['artists']),
                                            str(album_length),
                                            str(len(tracks_uri)))
            
            for track_uri, title, duration_ms in tracks_info:
                # if track_uri not in spotify_tracks: # condition doesn't matter
                spotify_tracks[track_uri] = (album['uri'],
                                             title,
                                             # Same as album artists, not always correct, but we don't iterate for every artist on every track.
                                             '; '.join(artist['name'] for artist in album['artists']), 
                                             str(duration_ms))
            
            # always make a note in log
            spotify_log.append((album['uri'],
                                spotify_playlist_id,
                                row['video_id'],
                                # 0, # category
                                str(album_num),
                                str(abs(diff)),
                                str(tracks_in_desc),
                                q,
                                search_type_id,
                                status))

            return True
        
        else:

            return False


def populate_spotify(row) -> None:
    """
    Find albums and tracks on Spotify, like or add them to the created playlists.

    Return:
        dict: a skeleton for df_spotify_catalog dataframe.
        If the first video is not found and populate_playlists returns None,
        df_spotify_catalog will be a Series.
    """
    spotify_playlist_id = None
    if row['playlist_name']:
        spotify_playlist_id = get_spotify_playlist_id(row)

    # ALBUM
    # THRESHOLD_MS is specified and the duration of the video is greater than or equal to it
    if os.getenv('THRESHOLD_MS') and row['duration_ms'] >= int(os.getenv('THRESHOLD_MS')):
        save_album(row, spotify_playlist_id)
    
    # TRACK
    # either THRESHOLD_MS is not specified or the duration of the video is less than it
    else:
        save_track(row, spotify_playlist_id)


def spotify_albums_to_df(spotify_albums: dict[str, tuple[str]]) -> pd.DataFrame:
    """
    Return a spotify album dataframe from a album dictionary.
    """
    df_spotify_albums = pd.DataFrame.from_dict(spotify_albums, orient='index',
                                               columns=['album_title',
                                                        'album_artists',
                                                        'duration_ms',
                                                        'total_tracks']) \
                                                .reset_index(names='album_uri')
    return df_spotify_albums


def spotify_tracks_to_df(spotify_tracks: dict[str, tuple[str]]) -> pd.DataFrame:
    """
    Return a spotify track dataframe from a track dictionary.
    """
    df_spotify_tracks = pd.DataFrame.from_dict(spotify_tracks, orient='index',
                                               columns=['album_uri',
                                                        'track_title',
                                                        'track_artists',
                                                        'duration_ms']) \
                                                .reset_index(names='track_uri')
    return df_spotify_tracks


def spotify_log_to_df(spotify_log: list[tuple[str]]) -> pd.DataFrame:
    """
    Return a spotify log dataframe from a log list.
    """
    df_spotify_log = pd.DataFrame(spotify_log, columns=['spotify_uri',
                                                        'spotify_playlist_id',
                                                        'youtube_video_id',
                                                        # 'category',
                                                        'found_on_try',
                                                        'difference_ms',
                                                        'tracks_in_desc',
                                                        'q',
                                                        'search_type_id',
                                                        'status'])

    return df_spotify_log


def create_df_search_types() -> pd.DataFrame:
    search_types = {'0': 'colons',
                    '1': 'title only',
                    '2': 'keyword and quotes',
                    '3': 'channel name and title'}
    
    df_search_types = pd.DataFrame.from_dict(search_types, orient='index',
                                            columns=['search_type_name']) \
                                            .reset_index(names='search_type_id')
    
    return df_search_types

if __name__ == '__main__':
    # Extract dataframes from BigQuery.
    df_playlists = extract_playlists()
    df_videos = extract_videos()
    print(f'Datasets were extracted from BigQuery.')

    # Authorisation.
    sp = get_authorization_code()
    user_id = get_user_id()

    # Create Spotify playlists.
    df_playlists['spotify_playlist_id'] = df_playlists.apply(create_spotify_playlists_from_df, axis = 1)
    print(f'{len(df_playlists)} playlists were added.')

    load_to_bigquery(df_playlists[['spotify_playlist_id', 'playlist_name']], 'spotify_playlists', 'replace')
    print(f'spotify_playlists uploaded to BigQuery.')
    load_to_bigquery(df_playlists[['youtube_playlist_id', 'spotify_playlist_id']], 'playlists_ids', 'replace')
    print(f'playlists_ids uploaded to BigQuery.')

    # Populate Spotify.
    spotify_albums: dict[str, tuple[str]] = {}
    spotify_tracks: dict[str, tuple[str]] = {}
    spotify_log: list[tuple[str]] = []

    df_videos.apply(populate_spotify, axis = 1)

    df_spotify_albums = spotify_albums_to_df(spotify_albums)
    load_to_bigquery(df_spotify_albums, 'spotify_albums', 'replace')
    print(f'spotify_albums uploaded to BigQuery, {len(df_spotify_albums)} rows.')

    df_spotify_tracks = spotify_tracks_to_df(spotify_tracks)
    load_to_bigquery(df_spotify_tracks, 'spotify_tracks', 'replace')
    print(f'spotify_tracks uploaded to BigQuery, {len(df_spotify_tracks)} rows.')

    df_spotify_log = spotify_log_to_df(spotify_log)
    load_to_bigquery(df_spotify_log, 'spotify_log', 'replace')
    print(f'spotify_log uploaded to BigQuery, {len(df_spotify_log)} rows.')

    # Create search types.
    df_search_types = create_df_search_types()
    load_to_bigquery(df_search_types, 'search_types', 'replace')
    print(f'search_types uploaded to BigQuery.')


# with DAG(
#     dag_id='request_access_token',
#     default_args=default_args,
#     description='Spotify DAG',
#     start_date=datetime(2023, 8, 11),
#     schedule='@hourly', # None
#     catchup=False,
# ) as dag:
    
#     request_access_token_task = PythonOperator(
#         task_id = 'request_access_token_task',
#         python_callable=request_access_token
#     )
    
    
#     request_access_token_task
