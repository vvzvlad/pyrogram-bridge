import logging
from feedgen.feed import FeedGenerator
from datetime import datetime, timezone
from typing import Optional
from post_parser import PostParser
from config import get_settings
from pyrogram import Client

Config = get_settings()

logger = logging.getLogger(__name__)


async def generate_channel_rss(channel: str, post_parser: Optional[PostParser] = None, client = None, limit: int = 20) -> str:
    """
    Generate RSS feed for channel using actual messages
    Args:
        channel: Telegram channel name
        post_parser: Optional PostParser instance. If not provided, will create new one
        client: Telegram client instance
        limit: Maximum number of posts to include in the RSS feed
    Returns:
        RSS feed as string in XML format
    """
    if limit < 1:
        raise ValueError(f"limit must be positive, got {limit}")
    if limit > 100:
        raise ValueError(f"limit cannot exceed 100, got {limit}")

    try:
        if post_parser is None:
            post_parser = PostParser(client=client)
            
        fg = FeedGenerator()
        fg.load_extension('dc')
        base_url = Config['pyrogram_bridge_url']
        
        try:
            channel_info = await post_parser.client.get_chat(channel)
            channel_title = channel_info.title or f"Telegram: {channel}"
            channel_icon = getattr(channel_info.photo, 'small_file_id', None) if channel_info.photo else None
        except Exception as e: # raise error if channel not found
            if "USERNAME_INVALID" in str(e):
                return create_error_feed(channel, base_url)
            else:
                raise

        # Set feed metadata
        main_name = f"{channel_title} (@{channel})"
        fg.title(main_name)
        fg.link(href=f"https://t.me/{channel}", rel='alternate')
        fg.description(f'Telegram channel {channel} RSS feed')
        fg.language('ru')
        #fg.dc.dc_creator(f"@{channel}")
        
        if channel_icon:
            fg.logo(f"{base_url}/media/{channel_icon}")
            fg.icon(f"{base_url}/media/{channel_icon}")
        
        fg.id(f"{base_url}/rss/{channel}")
        
        # First collect all posts
        posts = []
        media_groups = {}
        
        async for message in post_parser.client.get_chat_history(channel, limit=limit):
            try:
                # Skip service messages about pinned posts
                if message.service and 'PINNED_MESSAGE' in str(message.service):
                    logger.debug(f"Skipping pinned service message {message.id} in channel {channel}")
                    continue
                    
                naked_html = True if message.media_group_id else False
                post = post_parser.format_message_for_feed(message, naked=naked_html)
                if not post:
                    continue
                    
                if post.get('media_group_id'):
                    if post['media_group_id'] not in media_groups:
                        media_groups[post['media_group_id']] = []
                    media_groups[post['media_group_id']].append(post)
                else:
                    posts.append(post)
                    
            except Exception as e:
                logger.error(f"feed_entry_error: channel {channel}, message_id {message.id}, error {str(e)}")
                continue
        
        # Merge media groups
        for group_id, group_posts in media_groups.items():
            if not group_posts:
                continue
                
            # Find post with most meaningful title
            main_post = group_posts[0]
            for post in group_posts:
                current_title = post.get('title', '')
                if current_title and current_title not in ['📷 Photo', '📹 Video', '📄 Document']:
                    main_post = post
                    break
            
            merged_post = main_post.copy()
            merged_html = []
            
            for post in group_posts:
                merged_html.append(post['html'])
                
            merged_post['html'] = '\n'.join(merged_html)
            posts.append(merged_post)
            
        # Sort posts by date
        posts.sort(key=lambda x: x['date'], reverse=True)
        
        # Generate feed entries
        for post in posts:
            fe = fg.add_entry()
            fe.title(post.get('title', 'Untitled post'))
            
            post_link = f"https://t.me/{channel}/{post['message_id']}"
            fe.link(href=post_link)
            
            html_content = post.get('html', '')
            fe.description(f"<![CDATA[{html_content}]]>")
            fe.content(content=html_content, type='CDATA')
            
            pub_date = datetime.fromtimestamp(post['date'], tz=timezone.utc)
            fe.pubDate(pub_date)
            fe.guid(post_link, permalink=True)
            
            if post.get('author') and post['author'] != channel:
                fe.author(name="", email=post['author'])
                
        rss_feed = fg.rss_str(pretty=True)
        if isinstance(rss_feed, bytes):
            return rss_feed.decode('utf-8')
        return rss_feed
        
    except Exception as e:
        logger.error(f"rss_generation_error: channel {channel}, error {str(e)}")
        raise 


def create_error_feed(channel: str, base_url: str) -> str:
    """
    Create RSS feed with error message when channel is not found
    Args:
        channel: Telegram channel name
        base_url: Base URL for RSS feed
    Returns:
        RSS feed as string in XML format
    """
    fg = FeedGenerator()
    
    fg.title(f"Error: Channel @{channel} not found")
    fg.link(href=f"https://t.me/{channel}", rel='alternate')
    fg.description(f'Error: Telegram channel @{channel} does not exist')
    fg.language('en')
    fg.id(f"{base_url}/rss/{channel}")
    
    fe = fg.add_entry()
    fe.title("Channel not found")
    fe.link(href=f"https://t.me/{channel}")
    error_html = f"<p>The Telegram channel @{channel} does not exist or is not accessible.</p>"
    fe.description(f"<![CDATA[{error_html}]]>")
    fe.content(content=error_html, type='CDATA')
    fe.pubDate(datetime.now(tz=timezone.utc))
    fe.guid(f"https://t.me/{channel}", permalink=True)
    
    rss_feed = fg.rss_str(pretty=True)
    if isinstance(rss_feed, bytes):
        return rss_feed.decode('utf-8')
    return rss_feed