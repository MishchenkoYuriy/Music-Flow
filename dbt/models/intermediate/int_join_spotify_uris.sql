{{ config(materialized='ephemeral') }}

with

join_library_with_log as (

    select
        sl.*,
        yl.youtube_playlist_id,
        yl.video_id
    
    from {{ ref('stg__spotify_log') }} sl
    inner join {{ ref('stg__youtube_library') }} yl on sl.log_id = yl.id

),

join_uris as (

    select
        /* spotify_log */
        sl.log_id,
        sl.user_playlist_id, -- TODO
        sl.found_on_try,
        sl.difference_ms,
        sl.tracks_in_desc,
        sl.q,
        sl.search_type_id,
        sl.status,
        sl.added_at,

        /* youtube_videos */
        yv.video_id,
        yv.youtube_title,
        yv.youtube_channel,
        yv.description,
        yv.duration_ms as youtube_duration,

        /* others */
        sp.playlist_name,
        sty.search_type_name,

        /* spotify_albums or spotify_playlists_others or spotify_tracks */
        case
            when sl.album_uri is not null       then 'album'
            when sl.playlist_uri is not null   then 'playlist'
            when sl.track_uri is not null       then 'track'
        end as spotify_type,

        coalesce(sl.album_uri,      sl.playlist_uri,     sl.track_uri)      as spotify_uri,
        coalesce(sa.album_title,    spo.playlist_title,  st.track_title)    as spotify_title,
        coalesce(sa.album_artists,  spo.playlist_owner,  st.track_artists)  as spotify_artists,
        coalesce(sa.duration_ms,    spo.duration_ms,     st.duration_ms)    as spotify_duration,
        coalesce(sa.total_tracks,   spo.total_tracks,    1)                 as total_tracks
        
    from join_library_with_log sl
    inner join {{ ref('stg__youtube_videos') }} yv on sl.video_id = yv.video_id

    inner join {{ ref('stg__spotify_playlists') }} sp on sl.user_playlist_id = sp.spotify_playlist_id
    inner join {{ ref('stg__search_types') }} sty on sl.search_type_id = sty.search_type_id

    -- spotify_uri
    left join {{ ref('stg__spotify_albums')}} sa            on sl.album_uri = sa.album_uri
    left join {{ ref('stg__spotify_playlists_others')}} spo on sl.playlist_uri = spo.playlist_uri
    left join {{ ref('stg__spotify_tracks' )}} st           on sl.track_uri = st.track_uri

),

final as (

    select
        log_id,
        user_playlist_id, -- TODO
        found_on_try,
        difference_ms,
        tracks_in_desc,
        q,
        search_type_id,
        status,
        added_at,

        video_id,
        youtube_title,
        youtube_channel,
        description,
        youtube_duration,

        playlist_name,
        search_type_name,

        spotify_type,
        spotify_uri,
        spotify_title,
        spotify_artists,
        spotify_duration,
        total_tracks,

        round((tracks_in_desc / total_tracks) * 100, 1) as percentage_in_desc,

        time(timestamp_seconds(div(youtube_duration, 1000))) as youtube_duration_timestamp,
        time(timestamp_seconds(div(spotify_duration, 1000))) as spotify_duration_timestamp,
        round(difference_ms / 1000, 1) as difference_sec

    from join_uris

)

select * from final
