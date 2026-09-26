"""
DRPL Backend - Agent Testing Service
Test case management, execution, and automated evaluation for custom agents.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.models.agent_builder import CustomAgent, AgentTestCase, AgentTestRun
from app.services.agent_execution_service import execute_agent

logger = logging.getLogger(__name__)


# --- Test case CRUD ---

def create_test_case(db: Session, agent_id: int, data: dict,
                     user_id: Optional[int] = None) -> AgentTestCase:
    """
    Create a new test case for an agent.

    Args:
        db: Database session.
        agent_id: ID of the agent this test belongs to.
        data: Dict with test_name, input_data, expected_output, evaluation_criteria.
        user_id: ID of the creating user.

    Returns:
        The newly created AgentTestCase.
    """
    test_case = AgentTestCase(
        agent_id=agent_id,
        test_name=data["test_name"],
        input_data=data["input_data"],
        expected_output=data.get("expected_output"),
        evaluation_criteria=data.get("evaluation_criteria"),
        is_active=data.get("is_active", True),
        created_by=user_id,
    )
    db.add(test_case)
    db.commit()
    db.refresh(test_case)
    logger.info(f"Created test case '{test_case.test_name}' for agent_id={agent_id}")
    return test_case


def update_test_case(db: Session, test_id: int, data: dict) -> Optional[AgentTestCase]:
    """
    Update an existing test case.

    Returns:
        The updated AgentTestCase, or None if not found.
    """
    test_case = db.query(AgentTestCase).filter(AgentTestCase.id == test_id).first()
    if not test_case:
        return None

    for key, value in data.items():
        if hasattr(test_case, key) and key not in ("id", "agent_id", "created_by", "created_at"):
            setattr(test_case, key, value)

    db.commit()
    db.refresh(test_case)
    return test_case


def delete_test_case(db: Session, test_id: int) -> bool:
    """
    Delete a test case.

    Returns:
        True if deleted, False if not found.
    """
    test_case = db.query(AgentTestCase).filter(AgentTestCase.id == test_id).first()
    if not test_case:
        return False
    db.delete(test_case)
    db.commit()
    logger.info(f"Deleted test case id={test_id}")
    return True


def get_test_cases(db: Session, agent_id: int) -> list[AgentTestCase]:
    """List all test cases for an agent."""
    return (
        db.query(AgentTestCase)
        .filter(AgentTestCase.agent_id == agent_id)
        .order_by(AgentTestCase.test_name)
        .all()
    )


# --- Test execution ---

async def run_test_case(db: Session, test_case_id: int,
                        user_id: Optional[int] = None) -> AgentTestRun:
    """
    Execute a single test case against its agent and evaluate the output.

    Calls execute_agent with the test input, evaluates the output against
    the test's evaluation_criteria, and records an AgentTestRun.

    Args:
        db: Database session.
        test_case_id: ID of the test case to run.
        user_id: ID of the user triggering the run.

    Returns:
        The completed AgentTestRun record.
    """
    test_case = db.query(AgentTestCase).filter(AgentTestCase.id == test_case_id).first()
    if not test_case:
        raise ValueError(f"Test case not found: {test_case_id}")

    agent = db.query(CustomAgent).filter(CustomAgent.id == test_case.agent_id).first()
    if not agent:
        raise ValueError(f"Agent not found for test case: {test_case.agent_id}")

    # Execute the agent
    result = await execute_agent(
        db=db,
        agent_key_or_id=agent.id,
        input_data=test_case.input_data,
        user_id=user_id,
    )

    # Evaluate output
    output_text = result.get("output") or ""
    score, passed = _evaluate_output(output_text, test_case.evaluation_criteria)

    # Create test run record
    test_run = AgentTestRun(
        agent_id=agent.id,
        agent_version=agent.current_version,
        test_case_id=test_case.id,
        input_data=test_case.input_data,
        output_text=output_text[:5000] if output_text else None,
        tokens_input=result.get("tokens_input", 0),
        tokens_output=result.get("tokens_output", 0),
        latency_ms=result.get("latency_ms"),
        cost_estimate=result.get("cost_estimate", 0.0),
        score=score,
        passed=passed,
        error_message=result.get("error"),
        run_by=user_id,
    )
    db.add(test_run)
    db.commit()
    db.refresh(test_run)

    logger.info(
        f"Test run for case '{test_case.test_name}': "
        f"score={score}, passed={passed}, latency={result.get('latency_ms')}ms"
    )
    return test_run


async def run_all_tests(db: Session, agent_id: int,
                        user_id: Optional[int] = None) -> list[AgentTestRun]:
    """
    Run all active test cases for an agent.

    Args:
        db: Database session.
        agent_id: ID of the agent whose tests to run.
        user_id: ID of the user triggering the batch run.

    Returns:
        List of AgentTestRun records for each executed test.
    """
    test_cases = (
        db.query(AgentTestCase)
        .filter(AgentTestCase.agent_id == agent_id, AgentTestCase.is_active == True)
        .all()
    )

    results = []
    for tc in test_cases:
        try:
            test_run = await run_test_case(db, tc.id, user_id=user_id)
            results.append(test_run)
        except Exception as e:
            logger.error(f"Failed to run test case '{tc.test_name}' (id={tc.id}): {e}")
            # Record a failed run
            failed_run = AgentTestRun(
                agent_id=agent_id,
                test_case_id=tc.id,
                score=0.0,
                passed=False,
                error_message=str(e)[:1000],
                run_by=user_id,
            )
            db.add(failed_run)
            db.commit()
            db.refresh(failed_run)
            results.append(failed_run)

    logger.info(f"Batch test run for agent_id={agent_id}: {len(results)} tests executed")
    return results


# --- Evaluation ---

def _evaluate_output(output_text: str, criteria: Optional[dict]) -> tuple[float, bool]:
    """
    Evaluate agent output against evaluation criteria.

    Criteria format:
        {
            "must_contain": ["keyword1", "keyword2"],
            "must_not_contain": ["bad_word"],
            "score_threshold": 0.8
        }

    Returns:
        Tuple of (score: float 0-1, passed: bool).
    """
    if not criteria:
        # No criteria defined -- consider it passed with full score
        return 1.0, True

    output_lower = (output_text or "").lower()
    total_checks = 0
    passed_checks = 0

    # Check must_contain
    must_contain = criteria.get("must_contain", [])
    for keyword in must_contain:
        total_checks += 1
        if keyword.lower() in output_lower:
            passed_checks += 1

    # Check must_not_contain
    must_not_contain = criteria.get("must_not_contain", [])
    for keyword in must_not_contain:
        total_checks += 1
        if keyword.lower() not in output_lower:
            passed_checks += 1

    # Calculate score
    if total_checks == 0:
        score = 1.0
    else:
        score = round(passed_checks / total_checks, 4)

    # Determine pass/fail
    threshold = criteria.get("score_threshold", 0.8)
    passed = score >= threshold

    return score, passed
