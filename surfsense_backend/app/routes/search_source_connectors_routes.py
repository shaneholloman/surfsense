"""
SearchSourceConnector routes for CRUD operations:
POST /search-source-connectors/ - Create a new connector
GET /search-source-connectors/ - List all connectors for the current user
GET /search-source-connectors/{connector_id} - Get a specific connector
PUT /search-source-connectors/{connector_id} - Update a specific connector
DELETE /search-source-connectors/{connector_id} - Delete a specific connector
POST /search-source-connectors/{connector_id}/index - Index content from a connector to a search space

Note: Each user can have only one connector of each type (SERPER_API, TAVILY_API, SLACK_CONNECTOR, NOTION_CONNECTOR, GITHUB_CONNECTOR, LINEAR_CONNECTOR).
"""
from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.exc import IntegrityError
from typing import List, Dict, Any
from app.db import get_async_session, User, SearchSourceConnector, SearchSourceConnectorType, SearchSpace, async_session_maker
from app.schemas import SearchSourceConnectorCreate, SearchSourceConnectorUpdate, SearchSourceConnectorRead
from app.users import current_active_user
from app.utils.check_ownership import check_ownership
from pydantic import ValidationError
from app.tasks.connectors_indexing_tasks import index_slack_messages, index_notion_pages, index_github_repos, index_linear_issues
from datetime import datetime, timezone, timedelta
import logging

# Set up logging
logger = logging.getLogger(__name__)

router = APIRouter()

@router.post("/search-source-connectors/", response_model=SearchSourceConnectorRead)
async def create_search_source_connector(
    connector: SearchSourceConnectorCreate,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(current_active_user)
):
    """
    Create a new search source connector.
    
    Each user can have only one connector of each type (SERPER_API, TAVILY_API, SLACK_CONNECTOR, etc.).
    The config must contain the appropriate keys for the connector type.
    """
    try:
        # Check if a connector with the same type already exists for this user
        result = await session.execute(
            select(SearchSourceConnector)
            .filter(
                SearchSourceConnector.user_id == user.id,
                SearchSourceConnector.connector_type == connector.connector_type
            )
        )
        existing_connector = result.scalars().first()
        if existing_connector:
            raise HTTPException(
                status_code=409,
                detail=f"A connector with type {connector.connector_type} already exists. Each user can have only one connector of each type."
            )
        db_connector = SearchSourceConnector(**connector.model_dump(), user_id=user.id)
        session.add(db_connector)
        await session.commit()
        await session.refresh(db_connector)
        return db_connector
    except ValidationError as e:
        await session.rollback()
        raise HTTPException(
            status_code=422,
            detail=f"Validation error: {str(e)}"
        )
    except IntegrityError as e:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"Integrity error: A connector with this type already exists. {str(e)}"
        )
    except HTTPException:
        await session.rollback()
        raise
    except Exception as e:
        await session.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create search source connector: {str(e)}"
        )

@router.get("/search-source-connectors/", response_model=List[SearchSourceConnectorRead])
async def read_search_source_connectors(
    skip: int = 0,
    limit: int = 100,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(current_active_user)
):
    """List all search source connectors for the current user."""
    try:
        result = await session.execute(
            select(SearchSourceConnector)
            .filter(SearchSourceConnector.user_id == user.id)
            .offset(skip)
            .limit(limit)
        )
        return result.scalars().all()
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch search source connectors: {str(e)}"
        )

@router.get("/search-source-connectors/{connector_id}", response_model=SearchSourceConnectorRead)
async def read_search_source_connector(
    connector_id: int,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(current_active_user)
):
    """Get a specific search source connector by ID."""
    try:
        return await check_ownership(session, SearchSourceConnector, connector_id, user)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch search source connector: {str(e)}"
        )

@router.put("/search-source-connectors/{connector_id}", response_model=SearchSourceConnectorRead)
async def update_search_source_connector(
    connector_id: int,
    connector_update: SearchSourceConnectorUpdate,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(current_active_user)
):
    """
    Update a search source connector.
    
    Each user can have only one connector of each type (SERPER_API, TAVILY_API, SLACK_CONNECTOR, etc.).
    The config must contain the appropriate keys for the connector type.
    """
    try:
        db_connector = await check_ownership(session, SearchSourceConnector, connector_id, user)
        
        # If connector type is being changed, check if one of that type already exists
        if connector_update.connector_type != db_connector.connector_type:
            result = await session.execute(
                select(SearchSourceConnector)
                .filter(
                    SearchSourceConnector.user_id == user.id,
                    SearchSourceConnector.connector_type == connector_update.connector_type,
                    SearchSourceConnector.id != connector_id
                )
            )
            existing_connector = result.scalars().first()
            
            if existing_connector:
                raise HTTPException(
                    status_code=409,
                    detail=f"A connector with type {connector_update.connector_type} already exists. Each user can have only one connector of each type."
                )
        
        update_data = connector_update.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            setattr(db_connector, key, value)
        await session.commit()
        await session.refresh(db_connector)
        return db_connector
    except ValidationError as e:
        await session.rollback()
        raise HTTPException(
            status_code=422,
            detail=f"Validation error: {str(e)}"
        )
    except IntegrityError as e:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"Integrity error: A connector with this type already exists. {str(e)}"
        )
    except HTTPException:
        await session.rollback()
        raise
    except Exception as e:
        await session.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to update search source connector: {str(e)}"
        )

@router.delete("/search-source-connectors/{connector_id}", response_model=dict)
async def delete_search_source_connector(
    connector_id: int,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(current_active_user)
):
    """Delete a search source connector."""
    try:
        db_connector = await check_ownership(session, SearchSourceConnector, connector_id, user)
        await session.delete(db_connector)
        await session.commit()
        return {"message": "Search source connector deleted successfully"}
    except HTTPException:
        raise
    except Exception as e:
        await session.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to delete search source connector: {str(e)}"
        )

@router.post("/search-source-connectors/{connector_id}/index", response_model=Dict[str, Any])
async def index_connector_content(
    connector_id: int,
    search_space_id: int = Query(..., description="ID of the search space to store indexed content"),
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(current_active_user),
    background_tasks: BackgroundTasks = None
):
    """
    Index content from a connector to a search space.
    
    Currently supports:
    - SLACK_CONNECTOR: Indexes messages from all accessible Slack channels
    - NOTION_CONNECTOR: Indexes pages from all accessible Notion pages
    - GITHUB_CONNECTOR: Indexes code and documentation from GitHub repositories
    - LINEAR_CONNECTOR: Indexes issues and comments from Linear
    
    Args:
        connector_id: ID of the connector to use
        search_space_id: ID of the search space to store indexed content
        background_tasks: FastAPI background tasks
    
    Returns:
        Dictionary with indexing status
    """
    try:
        # Check if the connector belongs to the user
        connector = await check_ownership(session, SearchSourceConnector, connector_id, user)
        
        # Check if the search space belongs to the user
        search_space = await check_ownership(session, SearchSpace, search_space_id, user)
        
        # Handle different connector types
        response_message = ""
        indexing_from = None
        indexing_to = None
        today_str = datetime.now().strftime("%Y-%m-%d")

        if connector.connector_type == SearchSourceConnectorType.SLACK_CONNECTOR:
            # Determine the time range that will be indexed
            if not connector.last_indexed_at:
                start_date = "365 days ago" # Or perhaps set a specific date if needed
            else:
                # Check if last_indexed_at is today
                today = datetime.now().date()
                if connector.last_indexed_at.date() == today:
                    # If last indexed today, go back 1 day to ensure we don't miss anything
                    start_date = (today - timedelta(days=1)).strftime("%Y-%m-%d")
                else:
                    start_date = connector.last_indexed_at.strftime("%Y-%m-%d")
            
            indexing_from = start_date
            indexing_to = today_str
            
            # Run indexing in background
            logger.info(f"Triggering Slack indexing for connector {connector_id} into search space {search_space_id}")
            background_tasks.add_task(run_slack_indexing_with_new_session, connector_id, search_space_id)
            response_message = "Slack indexing started in the background."

        elif connector.connector_type == SearchSourceConnectorType.NOTION_CONNECTOR:
            # Determine the time range that will be indexed
            if not connector.last_indexed_at:
                start_date = "365 days ago" # Or perhaps set a specific date
            else:
                # Check if last_indexed_at is today
                today = datetime.now().date()
                if connector.last_indexed_at.date() == today:
                    # If last indexed today, go back 1 day to ensure we don't miss anything
                    start_date = (today - timedelta(days=1)).strftime("%Y-%m-%d")
                else:
                    start_date = connector.last_indexed_at.strftime("%Y-%m-%d")
            
            indexing_from = start_date
            indexing_to = today_str

            # Run indexing in background
            logger.info(f"Triggering Notion indexing for connector {connector_id} into search space {search_space_id}")
            background_tasks.add_task(run_notion_indexing_with_new_session, connector_id, search_space_id)
            response_message = "Notion indexing started in the background."
            
        elif connector.connector_type == SearchSourceConnectorType.GITHUB_CONNECTOR:
            # GitHub connector likely indexes everything relevant, or uses internal logic
            # Setting indexing_from to None and indexing_to to today
            indexing_from = None 
            indexing_to = today_str

            # Run indexing in background
            logger.info(f"Triggering GitHub indexing for connector {connector_id} into search space {search_space_id}")
            background_tasks.add_task(run_github_indexing_with_new_session, connector_id, search_space_id)
            response_message = "GitHub indexing started in the background."
            
        elif connector.connector_type == SearchSourceConnectorType.LINEAR_CONNECTOR:
            # Determine the time range that will be indexed
            if not connector.last_indexed_at:
                start_date = "365 days ago"
            else:
                # Check if last_indexed_at is today
                today = datetime.now().date()
                if connector.last_indexed_at.date() == today:
                    # If last indexed today, go back 1 day to ensure we don't miss anything
                    start_date = (today - timedelta(days=1)).strftime("%Y-%m-%d")
                else:
                    start_date = connector.last_indexed_at.strftime("%Y-%m-%d")
            
            indexing_from = start_date
            indexing_to = today_str

            # Run indexing in background
            logger.info(f"Triggering Linear indexing for connector {connector_id} into search space {search_space_id}")
            background_tasks.add_task(run_linear_indexing_with_new_session, connector_id, search_space_id)
            response_message = "Linear indexing started in the background."

        else:
            raise HTTPException(
                status_code=400,
                detail=f"Indexing not supported for connector type: {connector.connector_type}"
            )

        return {
            "message": response_message, 
            "connector_id": connector_id, 
            "search_space_id": search_space_id,
            "indexing_from": indexing_from,
            "indexing_to": indexing_to
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to initiate indexing for connector {connector_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to initiate indexing: {str(e)}"
        )
        
async def update_connector_last_indexed(
    session: AsyncSession,
    connector_id: int
):
    """
    Update the last_indexed_at timestamp for a connector.
    
    Args:
        session: Database session
        connector_id: ID of the connector to update
    """
    try:
        result = await session.execute(
            select(SearchSourceConnector)
            .filter(SearchSourceConnector.id == connector_id)
        )
        connector = result.scalars().first()
        
        if connector:
            connector.last_indexed_at = datetime.now()
            await session.commit()
            logger.info(f"Updated last_indexed_at for connector {connector_id}")
    except Exception as e:
        logger.error(f"Failed to update last_indexed_at for connector {connector_id}: {str(e)}")
        await session.rollback()

async def run_slack_indexing_with_new_session(
    connector_id: int,
    search_space_id: int
):
    """
    Create a new session and run the Slack indexing task.
    This prevents session leaks by creating a dedicated session for the background task.
    """
    async with async_session_maker() as session:
        await run_slack_indexing(session, connector_id, search_space_id)

async def run_slack_indexing(
    session: AsyncSession,
    connector_id: int,
    search_space_id: int
):
    """
    Background task to run Slack indexing.
    
    Args:
        session: Database session
        connector_id: ID of the Slack connector
        search_space_id: ID of the search space
    """
    try:
        # Index Slack messages without updating last_indexed_at (we'll do it separately)
        documents_processed, error_or_warning = await index_slack_messages(
            session=session,
            connector_id=connector_id,
            search_space_id=search_space_id,
            update_last_indexed=False  # Don't update timestamp in the indexing function
        )
        
        # Only update last_indexed_at if indexing was successful (either new docs or updated docs)
        if documents_processed > 0:
            await update_connector_last_indexed(session, connector_id)
            logger.info(f"Slack indexing completed successfully: {documents_processed} documents processed")
        else:
            logger.error(f"Slack indexing failed or no documents processed: {error_or_warning}")
    except Exception as e:
        logger.error(f"Error in background Slack indexing task: {str(e)}")

async def run_notion_indexing_with_new_session(
    connector_id: int,
    search_space_id: int
):
    """
    Create a new session and run the Notion indexing task.
    This prevents session leaks by creating a dedicated session for the background task.
    """
    async with async_session_maker() as session:
        await run_notion_indexing(session, connector_id, search_space_id)

async def run_notion_indexing(
    session: AsyncSession,
    connector_id: int,
    search_space_id: int
):
    """
    Background task to run Notion indexing.
    
    Args:
        session: Database session
        connector_id: ID of the Notion connector
        search_space_id: ID of the search space
    """
    try:
        # Index Notion pages without updating last_indexed_at (we'll do it separately)
        documents_processed, error_or_warning = await index_notion_pages(
            session=session,
            connector_id=connector_id,
            search_space_id=search_space_id,
            update_last_indexed=False  # Don't update timestamp in the indexing function
        )
        
        # Only update last_indexed_at if indexing was successful (either new docs or updated docs)
        if documents_processed > 0:
            await update_connector_last_indexed(session, connector_id)
            logger.info(f"Notion indexing completed successfully: {documents_processed} documents processed")
        else:
            logger.error(f"Notion indexing failed or no documents processed: {error_or_warning}")
    except Exception as e:
        logger.error(f"Error in background Notion indexing task: {str(e)}")

# Add new helper functions for GitHub indexing
async def run_github_indexing_with_new_session(
    connector_id: int,
    search_space_id: int
):
    """Wrapper to run GitHub indexing with its own database session."""
    logger.info(f"Background task started: Indexing GitHub connector {connector_id} into space {search_space_id}")
    async with async_session_maker() as session:
        await run_github_indexing(session, connector_id, search_space_id)
    logger.info(f"Background task finished: Indexing GitHub connector {connector_id}")

async def run_github_indexing(
    session: AsyncSession,
    connector_id: int,
    search_space_id: int
):
    """Runs the GitHub indexing task and updates the timestamp."""
    try:
        indexed_count, error_message = await index_github_repos(
            session, connector_id, search_space_id, update_last_indexed=False
        )
        if error_message:
            logger.error(f"GitHub indexing failed for connector {connector_id}: {error_message}")
            # Optionally update status in DB to indicate failure
        else:
            logger.info(f"GitHub indexing successful for connector {connector_id}. Indexed {indexed_count} documents.")
            # Update the last indexed timestamp only on success
            await update_connector_last_indexed(session, connector_id)
            await session.commit() # Commit timestamp update
    except Exception as e:
        await session.rollback()
        logger.error(f"Critical error in run_github_indexing for connector {connector_id}: {e}", exc_info=True)
        # Optionally update status in DB to indicate failure

# Add new helper functions for Linear indexing
async def run_linear_indexing_with_new_session(
    connector_id: int,
    search_space_id: int
):
    """Wrapper to run Linear indexing with its own database session."""
    logger.info(f"Background task started: Indexing Linear connector {connector_id} into space {search_space_id}")
    async with async_session_maker() as session:
        await run_linear_indexing(session, connector_id, search_space_id)
    logger.info(f"Background task finished: Indexing Linear connector {connector_id}")

async def run_linear_indexing(
    session: AsyncSession,
    connector_id: int,
    search_space_id: int
):
    """Runs the Linear indexing task and updates the timestamp."""
    try:
        indexed_count, error_message = await index_linear_issues(
            session, connector_id, search_space_id, update_last_indexed=False
        )
        if error_message:
            logger.error(f"Linear indexing failed for connector {connector_id}: {error_message}")
            # Optionally update status in DB to indicate failure
        else:
            logger.info(f"Linear indexing successful for connector {connector_id}. Indexed {indexed_count} documents.")
            # Update the last indexed timestamp only on success
            await update_connector_last_indexed(session, connector_id)
            await session.commit() # Commit timestamp update
    except Exception as e:
        await session.rollback()
        logger.error(f"Critical error in run_linear_indexing for connector {connector_id}: {e}", exc_info=True)
        # Optionally update status in DB to indicate failure
