from typing import Optional, List, Dict, Any, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.future import select
from datetime import datetime, timedelta
from app.db import Document, DocumentType, Chunk, SearchSourceConnector, SearchSourceConnectorType
from app.config import config
from app.prompts import SUMMARY_PROMPT_TEMPLATE
from app.connectors.slack_history import SlackHistory
from app.connectors.notion_history import NotionHistoryConnector
from slack_sdk.errors import SlackApiError
import logging

# Set up logging
logger = logging.getLogger(__name__)

async def index_slack_messages(
    session: AsyncSession,
    connector_id: int,
    search_space_id: int,
    update_last_indexed: bool = True
) -> Tuple[int, Optional[str]]:
    """
    Index Slack messages from all accessible channels.
    
    Args:
        session: Database session
        connector_id: ID of the Slack connector
        search_space_id: ID of the search space to store documents in
        update_last_indexed: Whether to update the last_indexed_at timestamp (default: True)
        
    Returns:
        Tuple containing (number of documents indexed, error message or None)
    """
    try:
        # Get the connector
        result = await session.execute(
            select(SearchSourceConnector)
            .filter(
                SearchSourceConnector.id == connector_id,
                SearchSourceConnector.connector_type == SearchSourceConnectorType.SLACK_CONNECTOR
            )
        )
        connector = result.scalars().first()
        
        if not connector:
            return 0, f"Connector with ID {connector_id} not found or is not a Slack connector"
        
        # Get the Slack token from the connector config
        slack_token = connector.config.get("SLACK_BOT_TOKEN")
        if not slack_token:
            return 0, "Slack token not found in connector config"
        
        # Initialize Slack client
        slack_client = SlackHistory(token=slack_token)
        
        # Calculate date range
        end_date = datetime.now()
        
        # Use last_indexed_at as start date if available, otherwise use 365 days ago
        if connector.last_indexed_at:
            # Check if last_indexed_at is today
            today = datetime.now().date()
            if connector.last_indexed_at.date() == today:
                # If last indexed today, go back 1 day to ensure we don't miss anything
                start_date = end_date - timedelta(days=7)
            else:
                start_date = connector.last_indexed_at
        else:
            start_date = end_date - timedelta(days=365)
        
        # Format dates for Slack API
        start_date_str = start_date.strftime("%Y-%m-%d")
        end_date_str = end_date.strftime("%Y-%m-%d")
        
        # Get all channels
        try:
            channels = slack_client.get_all_channels()
        except Exception as e:
            return 0, f"Failed to get Slack channels: {str(e)}"
        
        if not channels:
            return 0, "No Slack channels found"
        
        # Track the number of documents indexed
        documents_indexed = 0
        skipped_channels = []
        
        # Process each channel
        for channel_name, channel_id in channels.items():
            try:
                # Check if the bot is a member of the channel
                try:
                    # First try to get channel info to check if bot is a member
                    channel_info = slack_client.client.conversations_info(channel=channel_id)
                    
                    # For private channels, the bot needs to be a member
                    if channel_info.get("channel", {}).get("is_private", False):
                        # Check if bot is a member
                        is_member = channel_info.get("channel", {}).get("is_member", False)
                        if not is_member:
                            logger.warning(f"Bot is not a member of private channel {channel_name} ({channel_id}). Skipping.")
                            skipped_channels.append(f"{channel_name} (private, bot not a member)")
                            continue
                except SlackApiError as e:
                    if "not_in_channel" in str(e) or "channel_not_found" in str(e):
                        logger.warning(f"Bot cannot access channel {channel_name} ({channel_id}). Skipping.")
                        skipped_channels.append(f"{channel_name} (access error)")
                        continue
                    else:
                        # Re-raise if it's a different error
                        raise
                
                # Get messages for this channel
                messages, error = slack_client.get_history_by_date_range(
                    channel_id=channel_id,
                    start_date=start_date_str,
                    end_date=end_date_str,
                    limit=1000  # Limit to 1000 messages per channel
                )
                
                if error:
                    logger.warning(f"Error getting messages from channel {channel_name}: {error}")
                    skipped_channels.append(f"{channel_name} (error: {error})")
                    continue  # Skip this channel if there's an error
                
                if not messages:
                    logger.info(f"No messages found in channel {channel_name} for the specified date range.")
                    continue  # Skip if no messages
                
                # Format messages with user info
                formatted_messages = []
                for msg in messages:
                    # Skip bot messages and system messages
                    if msg.get("subtype") in ["bot_message", "channel_join", "channel_leave"]:
                        continue
                    
                    formatted_msg = slack_client.format_message(msg, include_user_info=True)
                    formatted_messages.append(formatted_msg)
                
                if not formatted_messages:
                    logger.info(f"No valid messages found in channel {channel_name} after filtering.")
                    continue  # Skip if no valid messages after filtering
                
                # Convert messages to markdown format
                channel_content = f"# Slack Channel: {channel_name}\n\n"
                
                for msg in formatted_messages:
                    user_name = msg.get("user_name", "Unknown User")
                    timestamp = msg.get("datetime", "Unknown Time")
                    text = msg.get("text", "")
                    
                    channel_content += f"## {user_name} ({timestamp})\n\n{text}\n\n---\n\n"
                
                # Format document metadata
                metadata_sections = [
                    ("METADATA", [
                        f"CHANNEL_NAME: {channel_name}",
                        f"CHANNEL_ID: {channel_id}",
                        f"START_DATE: {start_date_str}",
                        f"END_DATE: {end_date_str}",
                        f"MESSAGE_COUNT: {len(formatted_messages)}",
                        f"INDEXED_AT: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    ]),
                    ("CONTENT", [
                        "FORMAT: markdown",
                        "TEXT_START",
                        channel_content,
                        "TEXT_END"
                    ])
                ]
                
                # Build the document string
                document_parts = []
                document_parts.append("<DOCUMENT>")
                
                for section_title, section_content in metadata_sections:
                    document_parts.append(f"<{section_title}>")
                    document_parts.extend(section_content)
                    document_parts.append(f"</{section_title}>")
                
                document_parts.append("</DOCUMENT>")
                combined_document_string = '\n'.join(document_parts)
                
                # Generate summary
                summary_chain = SUMMARY_PROMPT_TEMPLATE | config.long_context_llm_instance
                summary_result = await summary_chain.ainvoke({"document": combined_document_string})
                summary_content = summary_result.content
                summary_embedding = config.embedding_model_instance.embed(summary_content)
                
                # Process chunks
                chunks = [
                    Chunk(content=chunk.text, embedding=chunk.embedding)
                    for chunk in config.chunker_instance.chunk(channel_content)
                ]
                
                # Create and store document
                document = Document(
                    search_space_id=search_space_id,
                    title=f"Slack - {channel_name}",
                    document_type=DocumentType.SLACK_CONNECTOR,
                    document_metadata={
                        "channel_name": channel_name,
                        "channel_id": channel_id,
                        "start_date": start_date_str,
                        "end_date": end_date_str,
                        "message_count": len(formatted_messages),
                        "indexed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    },
                    content=summary_content,
                    embedding=summary_embedding,
                    chunks=chunks
                )
                
                session.add(document)
                documents_indexed += 1
                logger.info(f"Successfully indexed channel {channel_name} with {len(formatted_messages)} messages")
                
            except SlackApiError as slack_error:
                logger.error(f"Slack API error for channel {channel_name}: {str(slack_error)}")
                skipped_channels.append(f"{channel_name} (Slack API error)")
                continue  # Skip this channel and continue with others
            except Exception as e:
                logger.error(f"Error processing channel {channel_name}: {str(e)}")
                skipped_channels.append(f"{channel_name} (processing error)")
                continue  # Skip this channel and continue with others
        
        # Update the last_indexed_at timestamp for the connector only if requested
        # and if we successfully indexed at least one channel
        if update_last_indexed and documents_indexed > 0:
            connector.last_indexed_at = datetime.now()
        
        # Commit all changes
        await session.commit()
        
        # Prepare result message
        result_message = None
        if skipped_channels:
            result_message = f"Indexed {documents_indexed} channels. Skipped {len(skipped_channels)} channels: {', '.join(skipped_channels)}"
        
        return documents_indexed, result_message
    
    except SQLAlchemyError as db_error:
        await session.rollback()
        logger.error(f"Database error: {str(db_error)}")
        return 0, f"Database error: {str(db_error)}"
    except Exception as e:
        await session.rollback()
        logger.error(f"Failed to index Slack messages: {str(e)}")
        return 0, f"Failed to index Slack messages: {str(e)}"

async def index_notion_pages(
    session: AsyncSession,
    connector_id: int,
    search_space_id: int,
    update_last_indexed: bool = True
) -> Tuple[int, Optional[str]]:
    """
    Index Notion pages from all accessible pages.
    
    Args:
        session: Database session
        connector_id: ID of the Notion connector
        search_space_id: ID of the search space to store documents in
        update_last_indexed: Whether to update the last_indexed_at timestamp (default: True)
        
    Returns:
        Tuple containing (number of documents indexed, error message or None)
    """
    try:
        # Get the connector
        result = await session.execute(
            select(SearchSourceConnector)
            .filter(
                SearchSourceConnector.id == connector_id,
                SearchSourceConnector.connector_type == SearchSourceConnectorType.NOTION_CONNECTOR
            )
        )
        connector = result.scalars().first()
        
        if not connector:
            return 0, f"Connector with ID {connector_id} not found or is not a Notion connector"
        
        # Get the Notion token from the connector config
        notion_token = connector.config.get("NOTION_INTEGRATION_TOKEN")
        if not notion_token:
            return 0, "Notion integration token not found in connector config"
        
        # Initialize Notion client
        logger.info(f"Initializing Notion client for connector {connector_id}")
        notion_client = NotionHistoryConnector(token=notion_token)
        
        # Calculate date range
        end_date = datetime.now()
        
        # Use last_indexed_at as start date if available, otherwise use 365 days ago
        if connector.last_indexed_at:
            # Check if last_indexed_at is today
            today = datetime.now().date()
            if connector.last_indexed_at.date() == today:
                # If last indexed today, go back 1 day to ensure we don't miss anything
                start_date = end_date - timedelta(days=1)
            else:
                start_date = connector.last_indexed_at
        else:
            start_date = end_date - timedelta(days=365)
        
        # Format dates for Notion API (ISO format)
        start_date_str = start_date.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_date_str = end_date.strftime("%Y-%m-%dT%H:%M:%SZ")
        
        logger.info(f"Fetching Notion pages from {start_date_str} to {end_date_str}")
        
        # Get all pages
        try:
            pages = notion_client.get_all_pages(start_date=start_date_str, end_date=end_date_str)
            logger.info(f"Found {len(pages)} Notion pages")
        except Exception as e:
            logger.error(f"Error fetching Notion pages: {str(e)}", exc_info=True)
            return 0, f"Failed to get Notion pages: {str(e)}"
        
        if not pages:
            logger.info("No Notion pages found to index")
            return 0, "No Notion pages found"
        
        # Track the number of documents indexed
        documents_indexed = 0
        skipped_pages = []
        
        # Process each page
        for page in pages:
            try:
                page_id = page.get("page_id")
                page_title = page.get("title", f"Untitled page ({page_id})")
                page_content = page.get("content", [])
                
                logger.info(f"Processing Notion page: {page_title} ({page_id})")
                
                if not page_content:
                    logger.info(f"No content found in page {page_title}. Skipping.")
                    skipped_pages.append(f"{page_title} (no content)")
                    continue
                
                # Convert page content to markdown format
                markdown_content = f"# Notion Page: {page_title}\n\n"
                
                # Process blocks recursively
                def process_blocks(blocks, level=0):
                    result = ""
                    for block in blocks:
                        block_type = block.get("type")
                        block_content = block.get("content", "")
                        children = block.get("children", [])
                        
                        # Add indentation based on level
                        indent = "  " * level
                        
                        # Format based on block type
                        if block_type in ["paragraph", "text"]:
                            result += f"{indent}{block_content}\n\n"
                        elif block_type in ["heading_1", "header"]:
                            result += f"{indent}# {block_content}\n\n"
                        elif block_type == "heading_2":
                            result += f"{indent}## {block_content}\n\n"
                        elif block_type == "heading_3":
                            result += f"{indent}### {block_content}\n\n"
                        elif block_type == "bulleted_list_item":
                            result += f"{indent}* {block_content}\n"
                        elif block_type == "numbered_list_item":
                            result += f"{indent}1. {block_content}\n"
                        elif block_type == "to_do":
                            result += f"{indent}- [ ] {block_content}\n"
                        elif block_type == "toggle":
                            result += f"{indent}> {block_content}\n"
                        elif block_type == "code":
                            result += f"{indent}```\n{block_content}\n```\n\n"
                        elif block_type == "quote":
                            result += f"{indent}> {block_content}\n\n"
                        elif block_type == "callout":
                            result += f"{indent}> **Note:** {block_content}\n\n"
                        elif block_type == "image":
                            result += f"{indent}![Image]({block_content})\n\n"
                        else:
                            # Default for other block types
                            if block_content:
                                result += f"{indent}{block_content}\n\n"
                        
                        # Process children recursively
                        if children:
                            result += process_blocks(children, level + 1)
                    
                    return result
                
                logger.debug(f"Converting {len(page_content)} blocks to markdown for page {page_title}")
                markdown_content += process_blocks(page_content)
                
                # Format document metadata
                metadata_sections = [
                    ("METADATA", [
                        f"PAGE_TITLE: {page_title}",
                        f"PAGE_ID: {page_id}",
                        f"INDEXED_AT: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    ]),
                    ("CONTENT", [
                        "FORMAT: markdown",
                        "TEXT_START",
                        markdown_content,
                        "TEXT_END"
                    ])
                ]
                
                # Build the document string
                document_parts = []
                document_parts.append("<DOCUMENT>")
                
                for section_title, section_content in metadata_sections:
                    document_parts.append(f"<{section_title}>")
                    document_parts.extend(section_content)
                    document_parts.append(f"</{section_title}>")
                
                document_parts.append("</DOCUMENT>")
                combined_document_string = '\n'.join(document_parts)
                
                # Generate summary
                logger.debug(f"Generating summary for page {page_title}")
                summary_chain = SUMMARY_PROMPT_TEMPLATE | config.long_context_llm_instance
                summary_result = await summary_chain.ainvoke({"document": combined_document_string})
                summary_content = summary_result.content
                summary_embedding = config.embedding_model_instance.embed(summary_content)
                
                # Process chunks
                logger.debug(f"Chunking content for page {page_title}")
                chunks = [
                    Chunk(content=chunk.text, embedding=chunk.embedding)
                    for chunk in config.chunker_instance.chunk(markdown_content)
                ]
                
                # Create and store document
                document = Document(
                    search_space_id=search_space_id,
                    title=f"Notion - {page_title}",
                    document_type=DocumentType.NOTION_CONNECTOR,
                    document_metadata={
                        "page_title": page_title,
                        "page_id": page_id,
                        "indexed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    },
                    content=summary_content,
                    embedding=summary_embedding,
                    chunks=chunks
                )
                
                session.add(document)
                documents_indexed += 1
                logger.info(f"Successfully indexed Notion page: {page_title}")
                
            except Exception as e:
                logger.error(f"Error processing Notion page {page.get('title', 'Unknown')}: {str(e)}", exc_info=True)
                skipped_pages.append(f"{page.get('title', 'Unknown')} (processing error)")
                continue  # Skip this page and continue with others
        
        # Update the last_indexed_at timestamp for the connector only if requested
        # and if we successfully indexed at least one page
        if update_last_indexed and documents_indexed > 0:
            connector.last_indexed_at = datetime.now()
            logger.info(f"Updated last_indexed_at for connector {connector_id}")
        
        # Commit all changes
        await session.commit()
        
        # Prepare result message
        result_message = None
        if skipped_pages:
            result_message = f"Indexed {documents_indexed} pages. Skipped {len(skipped_pages)} pages: {', '.join(skipped_pages)}"
        
        logger.info(f"Notion indexing completed: {documents_indexed} pages indexed, {len(skipped_pages)} pages skipped")
        return documents_indexed, result_message
    
    except SQLAlchemyError as db_error:
        await session.rollback()
        logger.error(f"Database error during Notion indexing: {str(db_error)}", exc_info=True)
        return 0, f"Database error: {str(db_error)}"
    except Exception as e:
        await session.rollback()
        logger.error(f"Failed to index Notion pages: {str(e)}", exc_info=True)
        return 0, f"Failed to index Notion pages: {str(e)}"
