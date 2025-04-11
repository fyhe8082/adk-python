# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

from typing import Any
from typing import AsyncGenerator
from typing import Callable
from typing import final
from typing import Optional
from typing import TYPE_CHECKING

from google.genai import types
from opentelemetry import trace
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator
from typing_extensions import override

from ..events.event import Event
from .callback_context import CallbackContext

if TYPE_CHECKING:
  from .invocation_context import InvocationContext

tracer = trace.get_tracer('gcp.vertex.agent')

BeforeAgentCallback = Callable[[CallbackContext], Optional[types.Content]]
"""Callback signature that is invoked before the agent run.

Args:
  callback_context: MUST be named 'callback_context' (enforced).

Returns:
  The content to return to the user. When set, the agent run will skipped and
  the provided content will be returned to user.
"""

AfterAgentCallback = Callable[[CallbackContext], Optional[types.Content]]
"""Callback signature that is invoked after the agent run.

Args:
  callback_context: MUST be named 'callback_context' (enforced).

Returns:
  The content to return to the user. When set, the agent run will skipped and
  the provided content will be appended to event history as agent response.
"""


class BaseAgent(BaseModel):
  """Base class for all agents in Agent Development Kit."""

  model_config = ConfigDict(
      arbitrary_types_allowed=True,
      extra='forbid',
  )

  name: str
  """The agent's name.

  Agent name must be a Python identifier and unique within the agent tree.
  Agent name cannot be "user", since it's reserved for end-user's input.
  """

  description: str = ''
  """Description about the agent's capability.

  The model uses this to determine whether to delegate control to the agent.
  One-line description is enough and preferred.
  """

  parent_agent: Optional[BaseAgent] = Field(default=None, init=False)
  """The parent agent of this agent.

  Note that an agent can ONLY be added as sub-agent once.

  If you want to add one agent twice as sub-agent, consider to create two agent
  instances with identical config, but with different name and add them to the
  agent tree.
  """
  sub_agents: list[BaseAgent] = Field(default_factory=list)
  """The sub-agents of this agent."""

  before_agent_callback: Optional[BeforeAgentCallback] = None
  """Callback signature that is invoked before the agent run.

  Args:
    callback_context: MUST be named 'callback_context' (enforced).

  Returns:
    The content to return to the user. When set, the agent run will skipped and
    the provided content will be returned to user.
  """
  after_agent_callback: Optional[AfterAgentCallback] = None
  """Callback signature that is invoked after the agent run.

  Args:
    callback_context: MUST be named 'callback_context' (enforced).

  Returns:
    The content to return to the user. When set, the agent run will skipped and
    the provided content will be appended to event history as agent response.
  """

  @final
  async def run_async(
      self,
      parent_context: InvocationContext,
  ) -> AsyncGenerator[Event, None]:
    """Entry method to run an agent via text-based conversation.

    Args:
      parent_context: InvocationContext, the invocation context of the parent
        agent.

    Yields:
      Event: the events generated by the agent.
    """

    with tracer.start_as_current_span(f'agent_run [{self.name}]'):
      ctx = self._create_invocation_context(parent_context)

      if event := self.__handle_before_agent_callback(ctx):
        yield event
      if ctx.end_invocation:
        return

      async for event in self._run_async_impl(ctx):
        yield event

      if ctx.end_invocation:
        return

      if event := self.__handle_after_agent_callback(ctx):
        yield event

  @final
  async def run_live(
      self,
      parent_context: InvocationContext,
  ) -> AsyncGenerator[Event, None]:
    """Entry method to run an agent via video/audio-based conversation.

    Args:
      parent_context: InvocationContext, the invocation context of the parent
        agent.

    Yields:
      Event: the events generated by the agent.
    """
    with tracer.start_as_current_span(f'agent_run [{self.name}]'):
      ctx = self._create_invocation_context(parent_context)
      # TODO(hangfei): support before/after_agent_callback

      async for event in self._run_live_impl(ctx):
        yield event

  async def _run_async_impl(
      self, ctx: InvocationContext
  ) -> AsyncGenerator[Event, None]:
    """Core logic to run this agent via text-based conversation.

    Args:
      ctx: InvocationContext, the invocation context for this agent.

    Yields:
      Event: the events generated by the agent.
    """
    raise NotImplementedError(
        f'_run_async_impl for {type(self)} is not implemented.'
    )
    yield  # AsyncGenerator requires having at least one yield statement

  async def _run_live_impl(
      self, ctx: InvocationContext
  ) -> AsyncGenerator[Event, None]:
    """Core logic to run this agent via video/audio-based conversation.

    Args:
      ctx: InvocationContext, the invocation context for this agent.

    Yields:
      Event: the events generated by the agent.
    """
    raise NotImplementedError(
        f'_run_live_impl for {type(self)} is not implemented.'
    )
    yield  # AsyncGenerator requires having at least one yield statement

  @property
  def root_agent(self) -> BaseAgent:
    """Gets the root agent of this agent."""
    root_agent = self
    while root_agent.parent_agent is not None:
      root_agent = root_agent.parent_agent
    return root_agent

  def find_agent(self, name: str) -> Optional[BaseAgent]:
    """Finds the agent with the given name in this agent and its descendants.

    Args:
      name: The name of the agent to find.

    Returns:
      The agent with the matching name, or None if no such agent is found.
    """
    if self.name == name:
      return self
    return self.find_sub_agent(name)

  def find_sub_agent(self, name: str) -> Optional[BaseAgent]:
    """Finds the agent with the given name in this agent's descendants.

    Args:
      name: The name of the agent to find.

    Returns:
      The agent with the matching name, or None if no such agent is found.
    """
    for sub_agent in self.sub_agents:
      if result := sub_agent.find_agent(name):
        return result
    return None

  def _create_invocation_context(
      self, parent_context: InvocationContext
  ) -> InvocationContext:
    """Creates a new invocation context for this agent."""
    invocation_context = parent_context.model_copy(update={'agent': self})
    if parent_context.branch:
      invocation_context.branch = f'{parent_context.branch}.{self.name}'
    return invocation_context

  def __handle_before_agent_callback(
      self, ctx: InvocationContext
  ) -> Optional[Event]:
    """Runs the before_agent_callback if it exists.

    Returns:
      Optional[Event]: an event if callback provides content or changed state.
    """
    ret_event = None

    if not isinstance(self.before_agent_callback, Callable):
      return ret_event

    callback_context = CallbackContext(ctx)
    before_agent_callback_content = self.before_agent_callback(
        callback_context=callback_context
    )

    if before_agent_callback_content:
      ret_event = Event(
          invocation_id=ctx.invocation_id,
          author=self.name,
          branch=ctx.branch,
          content=before_agent_callback_content,
          actions=callback_context._event_actions,
      )
      ctx.end_invocation = True
      return ret_event

    if callback_context.state.has_delta():
      ret_event = Event(
          invocation_id=ctx.invocation_id,
          author=self.name,
          branch=ctx.branch,
          actions=callback_context._event_actions,
      )

    return ret_event

  def __handle_after_agent_callback(
      self, invocation_context: InvocationContext
  ) -> Optional[Event]:
    """Runs the after_agent_callback if it exists.

    Returns:
      Optional[Event]: an event if callback provides content or changed state.
    """
    ret_event = None

    if not isinstance(self.after_agent_callback, Callable):
      return ret_event

    callback_context = CallbackContext(invocation_context)
    after_agent_callback_content = self.after_agent_callback(
        callback_context=callback_context
    )

    if after_agent_callback_content or callback_context.state.has_delta():
      ret_event = Event(
          invocation_id=invocation_context.invocation_id,
          author=self.name,
          branch=invocation_context.branch,
          content=after_agent_callback_content,
          actions=callback_context._event_actions,
      )

    return ret_event

  @override
  def model_post_init(self, __context: Any) -> None:
    self.__set_parent_agent_for_sub_agents()

  @field_validator('name', mode='after')
  @classmethod
  def __validate_name(cls, value: str):
    if not value.isidentifier():
      raise ValueError(
          f'Found invalid agent name: `{value}`.'
          ' Agent name must be a valid identifier. It should start with a'
          ' letter (a-z, A-Z) or an underscore (_), and can only contain'
          ' letters, digits (0-9), and underscores.'
      )
    if value == 'user':
      raise ValueError(
          "Agent name cannot be `user`. `user` is reserved for end-user's"
          ' input.'
      )
    return value

  def __set_parent_agent_for_sub_agents(self) -> BaseAgent:
    for sub_agent in self.sub_agents:
      if sub_agent.parent_agent is not None:
        raise ValueError(
            f'Agent `{sub_agent.name}` already has a parent agent, current'
            f' parent: `{sub_agent.parent_agent.name}`, trying to add:'
            f' `{self.name}`'
        )
      sub_agent.parent_agent = self
    return self
