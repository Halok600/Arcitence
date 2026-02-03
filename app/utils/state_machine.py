"""State machine validator for call states.

Ensures only valid state transitions occur, preventing data corruption.
"""

from typing import Optional, Set


class InvalidStateTransitionError(Exception):
    """Raised when attempting an invalid state transition."""

    def __init__(self, current_state: str, new_state: str, call_id: str):
        self.current_state = current_state
        self.new_state = new_state
        self.call_id = call_id
        super().__init__(
            f"Invalid state transition for call {call_id}: "
            f"{current_state} -> {new_state}"
        )


class CallStateMachine:
    """
    Enforces valid state transitions for calls.
    
    State diagram:
        IN_PROGRESS → COMPLETED → PROCESSING_AI → COMPLETED
                   ↓           ↗               ↘
                 FAILED ←────────────────────── FAILED
                   ↓                              ↓
                ARCHIVED ←──────────────────── ARCHIVED
    
    Design decisions:
    - Immutable state definitions (frozen set)
    - Explicit validation before database writes
    - Terminal state (ARCHIVED) cannot transition
    """

    # Valid state values
    STATES = frozenset(
        {
            "IN_PROGRESS",
            "COMPLETED",
            "PROCESSING_AI",
            "FAILED",
            "ARCHIVED",
        }
    )

    # Valid transitions mapping
    VALID_TRANSITIONS = {
        "IN_PROGRESS": {"COMPLETED", "FAILED"},
        "COMPLETED": {"PROCESSING_AI", "ARCHIVED"},
        "PROCESSING_AI": {"COMPLETED", "FAILED"},
        "FAILED": {"PROCESSING_AI", "ARCHIVED"},  # Allow retry
        "ARCHIVED": set(),  # Terminal state
    }

    @classmethod
    def validate_transition(
        cls, current_state: str, new_state: str, call_id: str
    ) -> None:
        """
        Validate a state transition.
        
        Args:
            current_state: Current state of the call
            new_state: Desired new state
            call_id: Call identifier for error reporting
            
        Raises:
            InvalidStateTransitionError: If transition is not allowed
            ValueError: If state values are invalid
        """
        # Validate state values
        if current_state not in cls.STATES:
            raise ValueError(f"Invalid current state: {current_state}")
        if new_state not in cls.STATES:
            raise ValueError(f"Invalid new state: {new_state}")

        # Allow no-op transitions (idempotency)
        if current_state == new_state:
            return

        # Check if transition is allowed
        allowed = cls.VALID_TRANSITIONS.get(current_state, set())
        if new_state not in allowed:
            raise InvalidStateTransitionError(current_state, new_state, call_id)

    @classmethod
    def can_transition(cls, current_state: str, new_state: str) -> bool:
        """
        Check if a transition is valid without raising an exception.
        
        Returns:
            True if transition is valid, False otherwise
        """
        try:
            cls.validate_transition(current_state, new_state, "unknown")
            return True
        except (InvalidStateTransitionError, ValueError):
            return False

    @classmethod
    def get_allowed_transitions(cls, current_state: str) -> Set[str]:
        """
        Get all allowed transitions from a given state.
        
        Args:
            current_state: The state to query
            
        Returns:
            Set of valid target states
        """
        return cls.VALID_TRANSITIONS.get(current_state, set()).copy()

    @classmethod
    def is_terminal_state(cls, state: str) -> bool:
        """
        Check if a state is terminal (no outgoing transitions).
        
        Args:
            state: State to check
            
        Returns:
            True if state is terminal
        """
        return len(cls.VALID_TRANSITIONS.get(state, set())) == 0
