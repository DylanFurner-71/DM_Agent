"""Agent-loop orchestration tests — no API (mocked client).

These drive DMAgent.take_turn / _execute / _maybe_end_combat / _closing_prompt and
assert on call routing, context building, and prompts. Moved out of test_rules.py."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src import rules, tools
from src.game_state import Character, NPC, GameState
from tests._helpers import _make_combat_state


def test_combat_loop_halts_at_player_no_skip():
    """No-API round-loop test.

    Scenario: initiative order Aldric (player) → Snik (NPC) → Wisp (player).
    After Aldric acts, the engine must:
      - advance to Snik and resolve his NPC turn (exactly one next_turn call)
      - advance to Wisp and halt (exactly one more next_turn call)
      - leave combat_index pointing at Wisp with combat_round still 1
    No extra next_turn that would push Wisp's slot into round 2.
    """
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    rules.seed(0)
    gs = GameState(location="Arena")
    # Extreme Dex values guarantee the order regardless of dice.
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 100})
    gs.npcs["snik"]   = NPC(name="Snik", max_hp=20, hp=20, ability_modifiers={"dex": 0})
    gs.party["wisp"]  = Character(name="Wisp", ability_modifiers={"dex": -100})

    res = tools.dispatch("start_combat", {"combatants": ["aldric", "snik", "wisp"]}, gs)
    assert res["combat_order"] == ["aldric", "snik", "wisp"], "precondition: known order"

    # Fake client always returns a plain text response — no tool calls, no API hit.
    fake_block = MagicMock()
    fake_block.type = "text"
    fake_block.text = "Narration."
    fake_resp = MagicMock()
    fake_resp.stop_reason = "end_turn"
    fake_resp.content = [fake_block]
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_resp

    agent = DMAgent(gs, client=fake_client)
    # Simulate Aldric having used his action (fake client makes no tool calls, so
    # action_used would otherwise stay False and the loop wouldn't advance past him).
    gs.action_used = True
    narration = agent.take_turn("Aldric swings at Snik")

    # Engine pointer must be on Wisp, still in round 1.
    assert gs.combat_order[gs.combat_index] == "wisp", (
        f"expected pointer on wisp, got {gs.combat_order[gs.combat_index]} "
        f"(round {gs.combat_round})"
    )
    assert gs.combat_round == 1, "extra next_turn pushed engine into round 2"

    # Authoritative turn line must name the engine's active combatant.
    assert "Wisp, what do you do?" in narration, "engine-sourced closing prompt missing or wrong name"

    # Exactly two next_turn advances: Aldric→Snik, Snik→Wisp.
    next_turns = [c for c in agent.tool_trace if c["name"] == "next_turn"]
    assert len(next_turns) == 2, f"expected 2 next_turn calls, got {len(next_turns)}"
    assert next_turns[0]["result"]["active"] == "snik"
    assert next_turns[1]["result"]["active"] == "wisp"




@pytest.mark.parametrize("first_key,second_key", [
    ("aldric", "wisp"),   # Aldric wins initiative — input names Wisp
    ("wisp", "aldric"),   # Wisp wins initiative  — input names Aldric
])
def test_loop_halts_at_first_player_when_input_names_another(first_key, second_key):
    """When a player is first in initiative and the input names a different player,
    the loop must halt at the first player and prompt for them — never advancing past.

    Covers the regression: start_combat sets active=first_player, turn guard rejects
    the named action (action_used stays False), loop must NOT call next_turn.
    """
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    rules.seed(0)
    gs = GameState(location="Arena")
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 100 if first_key == "aldric" else -100})
    gs.party["wisp"]   = Character(name="Wisp",   ability_modifiers={"dex": 100 if first_key == "wisp"   else -100}, spell_slots={1: 2})
    gs.npcs["snik"]    = NPC(name="Snik", max_hp=20, hp=20, ability_modifiers={"dex": 0})

    res = tools.dispatch("start_combat", {"combatants": ["aldric", "wisp", "snik"]}, gs)
    assert gs.combat_order[0] == first_key, "precondition: first_key won initiative"

    # Fake client: no tool calls — simulates model stopping after a turn-guard rejection.
    fake_block = MagicMock(); fake_block.type = "text"; fake_block.text = "Narration."
    fake_resp  = MagicMock(); fake_resp.stop_reason = "end_turn"; fake_resp.content = [fake_block]
    fake_client = MagicMock(); fake_client.messages.create.return_value = fake_resp

    agent = DMAgent(gs, client=fake_client)
    second_name = gs.party[second_key].name
    first_name  = gs.party[first_key].name

    narration = agent.take_turn(f"{second_name} casts magic missile at Snik")

    assert gs.combat_order[gs.combat_index] == first_key, (
        f"Loop advanced past {first_name} to {gs.combat_order[gs.combat_index]}"
    )
    assert f"{first_name}, what do you do?" in narration, (
        f"Expected prompt for {first_name}, got: {narration!r}"
    )
    next_turns = [c for c in agent.tool_trace if c["name"] == "next_turn"]
    assert next_turns == [], f"next_turn must not be called when active player hasn't acted; got {next_turns}"




def test_context_bounded_regardless_of_history_length():
    """_build_turn_context always returns at most 2*NARRATION_WINDOW messages,
    and uses the most recent entries, regardless of how many turns have passed."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent, NARRATION_WINDOW

    gs = GameState(location="Test")
    gs.party["aldric"] = Character(name="Aldric")
    agent = DMAgent(gs, client=MagicMock())

    # Simulate 20 turns of history — far beyond the window.
    for i in range(20):
        agent.narration_history.append((f"player did {i}", f"narration {i}"))

    messages = agent._build_turn_context()

    assert len(messages) == 2 * NARRATION_WINDOW
    # Most recent entries are at the end.
    assert "player did 19" in messages[-2]["content"]
    assert "narration 19" in messages[-1]["content"]
    # Oldest entries (0–15) are not present.
    assert all("player did 0" not in m["content"] for m in messages)




def test_state_snapshot_reflects_current_state_not_history():
    """_state_snapshot reads self.state directly, so it always shows the LATEST
    HP, spell slots, NPCs, and combat status — even when narration_history entries
    contain stale information from earlier turns."""
    import json as _json
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    gs = GameState(location="The Ember Chamber")
    gs.party["aldric"] = Character(
        name="Aldric", max_hp=24, hp=7,
        spell_slots={1: 0},
        conditions=["poisoned"],
    )
    gs.npcs["grik"] = NPC(name="Grik", max_hp=18, hp=5)
    gs.quest_flags["door_open"] = True
    gs.current_scene = "ember_chamber"

    agent = DMAgent(gs, client=MagicMock())
    # Add stale history with wrong HP to confirm the snapshot ignores it.
    agent.narration_history.append(("old input", "Aldric had 24/24 HP previously"))

    snap = _json.loads(agent._state_snapshot())

    assert snap["location"] == "The Ember Chamber"
    assert snap["current_scene"] == "ember_chamber"
    assert snap["party"]["Aldric"]["hp"] == "7/24"           # current, not stale
    assert snap["party"]["Aldric"]["conditions"] == ["poisoned"]
    assert snap["npcs"]["Grik"]["hp"] == "5/18"
    assert snap["quest_flags"]["door_open"] is True
    assert "combat" not in snap                              # not in combat




def test_tool_results_present_in_context_during_execute_loop():
    """Within a single turn's tool-use loop, the tool_result from the first API
    call must be present in the messages passed to the second API call.
    The between-turn context reset must NOT strip mid-turn results."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    gs = GameState(location="Test")
    gs.party["aldric"] = Character(name="Aldric")

    # Call 1 (_execute): model calls get_state
    tool_block = MagicMock()
    tool_block.type = "tool_use"; tool_block.id = "t1"
    tool_block.name = "get_state"; tool_block.input = {}
    exec_resp1 = MagicMock()
    exec_resp1.stop_reason = "tool_use"; exec_resp1.content = [tool_block]

    # Call 2 (_execute loop): model sees result and stops
    stop_block = MagicMock(); stop_block.type = "text"; stop_block.text = ""
    exec_resp2 = MagicMock()
    exec_resp2.stop_reason = "end_turn"; exec_resp2.content = [stop_block]

    # Call 3 (_narrate)
    narr_block = MagicMock(); narr_block.type = "text"; narr_block.text = "Done."
    narr_resp = MagicMock(); narr_resp.stop_reason = "end_turn"; narr_resp.content = [narr_block]

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [exec_resp1, exec_resp2, narr_resp]

    agent = DMAgent(gs, client=fake_client)
    agent.take_turn("Aldric looks around")

    # The second call (execute loop continuation) must carry the tool_result.
    call2_msgs = fake_client.messages.create.call_args_list[1][1]["messages"]
    tool_result_present = any(
        isinstance(m["content"], list) and
        any(isinstance(c, dict) and c.get("type") == "tool_result" for c in m["content"])
        for m in call2_msgs if m["role"] == "user"
    )
    assert tool_result_present, "tool_result must survive the within-turn loop — context reset is between turns only"




def test_npc_turns_batched_into_single_narration_call():
    """A cycle resolving [player, npc_A, npc_B, next_player] must produce exactly
    ONE narration API call — the unified turn narration that folds the player's
    action together with both NPC actions in resolution order — followed by one
    closing player prompt.

    With engine-resolved NPC turns, the two NPC turns generate ZERO additional
    execute (tool-use) API calls. Only the player's action needs the LLM for tool
    use. Both NPC attack results are injected into messages as context, and the
    single unified narration (folding player + NPC beats, Change 1+2) names both.

    Narration calls are identified as client.messages.create invocations that have
    no 'tools' kwarg; tool-use executions always pass tools=TOOLS.
    """
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    rules.seed(0)
    gs = GameState(location="Arena")
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 100})
    gs.npcs["snik"]    = NPC(name="Snik",  max_hp=20, hp=20, ability_modifiers={"dex": 0})
    gs.npcs["narl"]    = NPC(name="Narl",  max_hp=20, hp=20, ability_modifiers={"dex": -50})
    gs.party["wisp"]   = Character(name="Wisp",  ability_modifiers={"dex": -100})

    res = tools.dispatch("start_combat", {"combatants": ["aldric", "snik", "narl", "wisp"]}, gs)
    assert res["combat_order"] == ["aldric", "snik", "narl", "wisp"], "precondition: known order"

    fake_text = MagicMock()
    fake_text.type = "text"
    fake_text.text = "Narration."
    fake_resp = MagicMock()
    fake_resp.stop_reason = "end_turn"
    fake_resp.content = [fake_text]
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_resp

    agent = DMAgent(gs, client=fake_client)
    gs.action_used = True  # Aldric has used his action; loop must advance past him
    narration = agent.take_turn("Aldric swings at Snik")

    all_calls = fake_client.messages.create.call_args_list
    # Narration calls omit 'tools'; execute calls include it.
    narration_calls = [c for c in all_calls if "tools" not in c[1]]
    execute_calls   = [c for c in all_calls if "tools"     in c[1]]

    # Engine-resolved NPCs add zero execute calls. Only the player's action uses the LLM.
    assert len(execute_calls) == 1, (
        f"expected 1 execute call (player action only; NPC turns engine-resolved), "
        f"got {len(execute_calls)}"
    )

    assert len(narration_calls) == 1, (
        f"expected 1 unified narration call, got {len(narration_calls)} "
        f"(total API calls: {len(all_calls)})"
    )

    # The unified narration prompt must cover the player's action AND both NPCs.
    batch_messages = narration_calls[0][1]["messages"]
    last_user_prompt = next(
        m["content"] for m in reversed(batch_messages)
        if m["role"] == "user" and isinstance(m["content"], str)
    )
    assert "Aldric swings at Snik" in last_user_prompt, "unified prompt must include the player's action"
    assert "Snik" in last_user_prompt, "unified prompt must name Snik"
    assert "Narl" in last_user_prompt, "unified prompt must name Narl"

    # Engine-sourced closing prompt must address the next active player.
    assert "Wisp, what do you do?" in narration

    # api_stats must contain exactly ONE "thinking" entry (the player's action).
    # Without engine resolution the old code would have 3 (player + Snik + Narl).
    thinking_stats = [s for s in agent.api_stats if s["phase"] == "thinking"]
    assert len(thinking_stats) == 1, (
        f"expected 1 thinking call (player action only); "
        f"engine-resolved NPC turns must not add thinking entries. got {len(thinking_stats)}"
    )




def test_end_combat_triggers_post_combat_narration():
    """When end_combat fires during _execute, take_turn must:
    - call _narrate_combat_over (not _narrate) for that action
    - leave combat state cleared (combat_round == 0)
    - NOT append a combat-turn prompt
    """
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    rules.seed(0)
    gs = _make_combat_state()
    gs.party["aldric"].ability_modifiers["dex"] = 100
    tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)
    gs.action_used = True  # Aldric has acted

    # API call sequence (combat player-turn fast path):
    # 1. _execute: model calls end_combat tool. action_used is already True, so
    #    _execute breaks here — no terminal model turn is spent (and would be scrubbed).
    ec_block = MagicMock()
    ec_block.type = "tool_use"; ec_block.id = "t1"
    ec_block.name = "end_combat"; ec_block.input = {}
    exec_resp = MagicMock()
    exec_resp.stop_reason = "tool_use"; exec_resp.content = [ec_block]

    # 2. _narrate_combat_over: post-combat prose
    narr_block = MagicMock()
    narr_block.type = "text"
    narr_block.text = "The goblin crumples. Silence falls over the chamber. The passage yawns ahead. What do you do?"
    narr_resp = MagicMock(); narr_resp.stop_reason = "end_turn"; narr_resp.content = [narr_block]

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [exec_resp, narr_resp]

    agent = DMAgent(gs, client=fake_client)
    narration = agent.take_turn("Aldric finishes the goblin")

    # Combat state cleared
    assert gs.combat_round == 0
    assert gs.combat_order == []

    # Post-combat narration was produced (contains the exploration prompt)
    assert "What do you do?" in narration

    # _narrate_combat_over was used: its prompt contains "Combat is over"
    narrate_call_messages = fake_client.messages.create.call_args_list[1][1]["messages"]
    last_user_content = next(
        m["content"] for m in reversed(narrate_call_messages) if m["role"] == "user"
    )
    assert "Combat is over" in str(last_user_content)

    # No combat-turn prompt appended after end_combat
    assert "Aldric, what do you do?" not in narration
    assert "Snik, what do you do?" not in narration




def test_agent_re_prompts_after_failed_cast_not_success():
    """When cast_spell returns ok=False (no slots), the agent must feed the failure
    result back to the model before requesting narration — never silently succeeding.

    Structural assertions:
    - Engine did not apply damage (HP unchanged).
    - Tool trace records ok=False.
    - The second API call (the terminating turn) carries a tool_result whose JSON
      content contains 'ok': false — proving the failure was fed back before the
      model narrated.
    - Exactly two API calls: out of combat the terminating turn IS the narration
      (merge of Change 1), so no separate narration call is spent.
    """
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    gs = GameState(location="Dungeon")
    gs.party["wisp"] = Character(name="Wisp", spell_slots={1: 0})  # no slots remaining
    gs.npcs["snik"] = NPC(name="Snik", max_hp=10, hp=10)

    # Call 1 (_execute): model tries cast_spell
    cs_block = MagicMock()
    cs_block.type = "tool_use"; cs_block.id = "t1"
    cs_block.name = "cast_spell"
    cs_block.input = {"caster": "Wisp", "spell_level": 1, "spell_name": "magic_missile", "target": "Snik"}
    exec_resp = MagicMock()
    exec_resp.stop_reason = "tool_use"; exec_resp.content = [cs_block]

    # Call 2 (terminating turn): model sees ok=False and narrates the fizzle in its
    # final message — out of combat this terminal response IS the narration.
    narr_block = MagicMock()
    narr_block.type = "text"
    narr_block.text = "Wisp reaches for the magic but finds only silence — the spell fizzles."
    narr_resp = MagicMock(); narr_resp.stop_reason = "end_turn"; narr_resp.content = [narr_block]

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [exec_resp, narr_resp]

    agent = DMAgent(gs, client=fake_client)
    narration = agent.take_turn("Wisp casts magic missile at Snik")

    # Engine enforced the failure — no HP change.
    assert gs.npcs["snik"].hp == 10

    # Tool trace records ok=False.
    cast_calls = [c for c in agent.tool_trace if c["name"] == "cast_spell"]
    assert len(cast_calls) == 1
    assert cast_calls[0]["result"]["ok"] is False

    # The second API call must carry the tool_result with the ok=False payload.
    # This is the "re-prompt": the agent fed the failure back before the model narrated.
    call2_msgs = fake_client.messages.create.call_args_list[1][1]["messages"]
    tool_result_content = next(
        (c["content"]
         for m in call2_msgs if m["role"] == "user" and isinstance(m["content"], list)
         for c in m["content"] if isinstance(c, dict) and c.get("type") == "tool_result"),
        None,
    )
    assert tool_result_content is not None, "agent must feed tool_result back before narrating"
    assert '"ok": false' in tool_result_content, (
        f"tool_result must contain ok=false, got: {tool_result_content!r}"
    )

    # The terminating turn was captured as the narration.
    assert "fizzles" in narration

    # Merge: terminating turn IS the narration — 2 calls, not 3.
    assert fake_client.messages.create.call_count == 2




def test_invalid_action_does_not_advance_turn():
    """Attack rejected for an unowned weapon must:
    - leave the active combatant unchanged (no next_turn fired)
    - leave the round number unchanged
    - leave action_used=False so a follow-up valid attack by the same character succeeds.
    """
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    rules.seed(0)
    gs = GameState(location="Arena")
    # Aldric goes first (extreme dex) and carries only a mace — no dagger.
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 100},
                                   inventory=["mace"])
    gs.npcs["snik"] = NPC(name="Snik", max_hp=20, hp=20, ability_modifiers={"dex": 0})

    res = tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)
    assert gs.combat_order[0] == "aldric"

    # Fake client: model calls attack(dagger) → rejected; stops; narrates rejection.
    dagger_block = MagicMock()
    dagger_block.type = "tool_use"; dagger_block.id = "t1"
    dagger_block.name = "attack"
    dagger_block.input = {"attacker": "Aldric", "defender": "Snik", "weapon": "dagger"}
    exec_resp = MagicMock()
    exec_resp.stop_reason = "tool_use"; exec_resp.content = [dagger_block]

    stop_block = MagicMock(); stop_block.type = "text"; stop_block.text = ""
    stop_resp = MagicMock(); stop_resp.stop_reason = "end_turn"; stop_resp.content = [stop_block]

    narr_block = MagicMock()
    narr_block.type = "text"
    narr_block.text = "Aldric has no dagger — he's carrying a mace. What does Aldric do?"
    narr_resp = MagicMock(); narr_resp.stop_reason = "end_turn"; narr_resp.content = [narr_block]

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [exec_resp, stop_resp, narr_resp]

    agent = DMAgent(gs, client=fake_client)
    agent.take_turn("Aldric attacks Snik with dagger")

    # Active combatant and round unchanged.
    assert gs.combat_order[gs.combat_index] == "aldric", (
        f"pointer moved to {gs.combat_order[gs.combat_index]} after invalid action"
    )
    assert gs.combat_round == 1, "round advanced after invalid action"
    assert gs.action_used is False, "action_used must be False — no valid action was taken"

    # No next_turn must have fired.
    next_turns = [c for c in agent.tool_trace if c["name"] == "next_turn"]
    assert next_turns == [], f"next_turn fired {len(next_turns)} time(s) after invalid action"

    # Follow-up: the same character's valid attack resolves normally.
    follow_up = tools.dispatch(
        "attack", {"attacker": "Aldric", "defender": "Snik", "weapon": "mace"}, gs
    )
    assert follow_up["ok"] is True, "valid follow-up attack must succeed"
    assert gs.action_used is True  # action now consumed by the valid attack




# --- engine-driven end-of-combat detection -----------------------------------

def test_engine_auto_ends_combat_when_last_enemy_downed():
    """Player attack downs the last hostile → _maybe_end_combat fires, combat_round=0,
    take_turn routes to the post-combat wrap-up, no combat-turn closing prompt emitted.
    Scene has exits so the run does not end here (game_over stays False)."""
    from unittest.mock import MagicMock, patch
    from src.dm_agent import DMAgent

    rules.seed(0)
    gs = GameState(location="Arena", current_scene="arena")
    gs.scenes = {
        "arena": {"location": "Arena", "exits": {"the vault ahead": "vault"}},
        "vault": {"location": "The Vault", "exits": {}},
    }
    gs.party["aldric"] = Character(
        name="Aldric",
        ability_modifiers={"str": 5, "dex": 100},
        inventory=["mace"],
        proficiency_bonus=2,
    )
    gs.npcs["snik"] = NPC(name="Snik", max_hp=1, hp=1, ac=1)

    tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)
    assert gs.combat_order[0] == "aldric"

    # Model calls attack(Aldric, mace) — engine auto-selects Snik
    atk_block = MagicMock()
    atk_block.type = "tool_use"; atk_block.id = "t1"
    atk_block.name = "attack"
    atk_block.input = {"attacker": "Aldric", "weapon": "mace"}

    exec_resp = MagicMock()
    exec_resp.stop_reason = "tool_use"; exec_resp.content = [atk_block]

    # No terminal model turn: attack sets action_used, so _execute breaks right after
    # the tool resolves. The engine (not the model) ends combat via _maybe_end_combat
    # while combat_round is still > 0.
    narr_block = MagicMock()
    narr_block.type = "text"
    narr_block.text = "Silence falls. The passage yawns ahead. What do you do?"
    narr_resp = MagicMock(); narr_resp.stop_reason = "end_turn"; narr_resp.content = [narr_block]

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [exec_resp, narr_resp]

    agent = DMAgent(gs, client=fake_client)

    # d20=15 (hit), 1d6=3 (damage → 3+5=8 > Snik's 1 HP)
    with patch.object(rules._rng, "randint", side_effect=[15, 3]):
        narration = agent.take_turn("Aldric strikes Snik")

    assert gs.npcs["snik"].hp == 0 and gs.npcs["snik"].is_down

    # Engine ended combat automatically
    assert gs.combat_round == 0
    assert gs.combat_order == []
    ec_calls = [c for c in agent.tool_trace if c["name"] == "end_combat"]
    assert len(ec_calls) == 1

    # _narrate_combat_over was used (its prompt contains "Combat is over")
    narrate_call_msgs = fake_client.messages.create.call_args_list[1][1]["messages"]
    last_user_content = next(
        m["content"] for m in reversed(narrate_call_msgs) if m["role"] == "user"
    )
    assert "Combat is over" in str(last_user_content)

    # No combat-turn prompt appended
    assert "Aldric, what do you do?" not in narration
    assert "Snik, what do you do?" not in narration




def test_combat_pc_action_narrated_when_next_combatant_is_pc():
    """Regression: in combat, a PC's action must be narrated even when the NEXT
    combatant in initiative is another conscious PC (so no NPC beats accumulate).

    The bug: narration was gated on combat_beats being non-empty. When a PC acted
    and the following combatant was also a PC, the NPC loop advanced the pointer and
    broke immediately, producing zero beats — so _narrate_turn was never called and
    the player's own action went un-narrated (only a 'thinking' API call, no
    narration). Observed across Wisp's turns in the order wisp -> aldric -> grik -> narl.
    """
    from unittest.mock import MagicMock, patch
    from src.dm_agent import DMAgent

    gs = GameState(location="Arena")
    gs.party["wisp"] = Character(name="Wisp", spell_slots={1: 1}, spells=["magic_missile"],
                                 spellcasting_ability="int", ability_modifiers={"int": 4})
    gs.party["aldric"] = Character(name="Aldric", inventory=["mace"], ability_modifiers={"str": 2})
    gs.npcs["snik"] = NPC(name="Snik", max_hp=30, hp=30, ac=10)

    # Force the order so Wisp acts, then a PC (Aldric) is next — no NPC beats this turn.
    tools.dispatch("start_combat", {"combatants": ["wisp", "aldric", "snik"]}, gs)
    gs.combat_order = ["wisp", "aldric", "snik"]
    gs.combat_index = 0
    gs.combat_round = 1
    gs.action_used = False
    gs.combat_starting = False

    # _execute: model casts magic_missile at Snik; action_used set -> _execute breaks
    # (no terminal model turn). One create() call for the tool-use phase.
    cast_block = MagicMock()
    cast_block.type = "tool_use"; cast_block.id = "t1"
    cast_block.name = "cast_spell"
    cast_block.input = {"caster": "Wisp", "spell_level": 1, "spell_name": "magic_missile", "target": "Snik"}
    exec_resp = MagicMock(); exec_resp.stop_reason = "tool_use"; exec_resp.content = [cast_block]

    narr_block = MagicMock(); narr_block.type = "text"
    narr_block.text = "Three darts of force slam into Snik."
    narr_resp = MagicMock(); narr_resp.stop_reason = "end_turn"; narr_resp.content = [narr_block]

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [exec_resp, narr_resp]
    agent = DMAgent(gs, client=fake_client)

    with patch.object(rules._rng, "randint", side_effect=[2, 2, 2]):  # 3d4 = 6, +3 = 9 dmg
        narration = agent.take_turn("Wisp fires magic missile at Snik")

    # Combat continues — Snik survived, round still > 0.
    assert gs.npcs["snik"].hp == 21
    assert gs.combat_round > 0
    # A narration call WAS made — the bug skipped it entirely (only the tool-use call).
    assert fake_client.messages.create.call_count == 2
    # The player's action narration reaches the player.
    assert "Three darts of force" in narration
    # _narrate_turn was the vehicle (its prompt enumerates beats; the player action is beat 1).
    narr_prompt = str(fake_client.messages.create.call_args_list[1][1]["messages"])
    assert "Narrate each of the following" in narr_prompt
    # The closing prompt addresses the next actor (Aldric).
    assert "Aldric" in narration




def test_model_end_combat_in_terminal_scene_still_fires_victory_epilogue():
    """Regression: a *model*-issued end_combat in a terminal scene must still declare
    victory and fire the epilogue.

    The bug: dispatch('end_combat') zeroes combat_round, so the subsequent
    _maybe_end_combat() short-circuited on its combat_round==0 guard and never set
    game_over — leaving the run stuck in a terminal scene with the epilogue unfired.
    The verdict now lives in _adjudicate_combat_outcome, called regardless of who
    ended combat, so the epilogue fires either way.
    """
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    rules.seed(0)
    gs = GameState(location="The Vault", current_scene="vault")
    gs.scenes = {"vault": {"location": "The Vault", "exits": {}}}  # terminal: no exits
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 100})
    gs.npcs["snik"] = NPC(name="Snik", max_hp=6, hp=0)  # last hostile already down

    tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)
    gs.action_used = True  # Aldric has acted; the model now wraps up the fight

    # 1. _execute: model calls end_combat itself (the bug trigger). action_used is set,
    #    so _execute breaks here — no terminal model turn.
    ec_block = MagicMock()
    ec_block.type = "tool_use"; ec_block.id = "t1"
    ec_block.name = "end_combat"; ec_block.input = {}
    exec_resp = MagicMock()
    exec_resp.stop_reason = "tool_use"; exec_resp.content = [ec_block]

    # 2. _narrate_epilogue: the triumphant closing paragraph.
    epi_block = MagicMock()
    epi_block.type = "text"
    epi_block.text = "The vault falls silent. The party stands victorious amid the dust."
    epi_resp = MagicMock(); epi_resp.stop_reason = "end_turn"; epi_resp.content = [epi_block]

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [exec_resp, epi_resp]

    agent = DMAgent(gs, client=fake_client)
    narration = agent.take_turn("Aldric ends the fight")

    # Verdict declared despite the model — not the engine — ending combat.
    assert gs.game_over is True
    assert gs.game_outcome == "victory"
    assert gs.combat_round == 0 and gs.combat_order == []

    # The epilogue fired: its prose is returned, and the second call used the
    # victory-epilogue prompt ("the party has prevailed").
    assert "victorious" in narration
    epi_call_msgs = fake_client.messages.create.call_args_list[1][1]["messages"]
    last_user = next(m["content"] for m in reversed(epi_call_msgs) if m["role"] == "user")
    assert "the party has prevailed" in str(last_user)




def test_start_combat_prints_initiative_banner_once():
    """A plain start_combat must print a deterministic initiative readout to the player
    once, mirroring the ambush announcement — built by the engine, not the model."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    rules.seed(0)
    gs = GameState(location="Cave", current_scene="cave")
    gs.scenes = {"cave": {"location": "Cave", "exits": {"out": "exit"}}}
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 100})  # leads
    gs.npcs["snik"] = NPC(name="Snik", max_hp=10, hp=10, ability_modifiers={"dex": -5})

    # 1. _execute: model calls start_combat (terminal call for this input).
    sc_block = MagicMock()
    sc_block.type = "tool_use"; sc_block.id = "t1"
    sc_block.name = "start_combat"; sc_block.input = {"combatants": ["Aldric", "Snik"]}
    exec_resp = MagicMock()
    exec_resp.stop_reason = "tool_use"; exec_resp.content = [sc_block]

    # 2. terminating turn: the model narrates the outbreak (no initiative list).
    narr_block = MagicMock()
    narr_block.type = "text"
    narr_block.text = "Steel rasps free as the goblin lunges."
    narr_resp = MagicMock(); narr_resp.stop_reason = "end_turn"; narr_resp.content = [narr_block]

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [exec_resp, narr_resp]

    agent = DMAgent(gs, client=fake_client)
    narration = agent.take_turn("Aldric attacks the goblin")

    # Deterministic banner present, in initiative order, exactly once.
    assert "**Initiative order:** Aldric → Snik" in narration
    assert narration.count("Initiative order:") == 1
    # It is a trailer, not stored in the rolling narration history.
    assert "Initiative order" not in agent.narration_history[-1][1]




def test_maybe_end_combat_clears_when_last_hostile_down():
    """Sole hostile already at 0 HP → returns True, combat_round cleared to 0."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    rules.seed(0)
    gs = GameState(location="Test")
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 0})
    gs.npcs["snik"] = NPC(name="Snik", max_hp=12, hp=0)  # already downed
    tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)
    assert gs.combat_round == 1

    agent = DMAgent(gs, client=MagicMock())
    ended = agent._maybe_end_combat()

    assert ended is True
    assert gs.combat_round == 0
    assert gs.combat_order == []




def test_maybe_end_combat_continues_with_living_hostile():
    """Two hostiles, one downed — one still alive → returns False, combat unchanged."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    rules.seed(0)
    gs = GameState(location="Test")
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 0})
    gs.npcs["snik"] = NPC(name="Snik", max_hp=12, hp=0)    # downed
    gs.npcs["narl"] = NPC(name="Narl", max_hp=12, hp=12)   # alive
    tools.dispatch("start_combat", {"combatants": ["aldric", "snik", "narl"]}, gs)
    round_before = gs.combat_round

    agent = DMAgent(gs, client=MagicMock())
    ended = agent._maybe_end_combat()

    assert ended is False
    assert gs.combat_round == round_before
    assert agent.tool_trace == []




def test_maybe_end_combat_ends_on_party_wipe():
    """All PCs at 0 HP with a hostile still alive → returns True, combat ends (symmetric defeat)."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    rules.seed(0)
    gs = GameState(location="Test")
    gs.party["aldric"] = Character(name="Aldric", max_hp=24, hp=0, ability_modifiers={"dex": 0})
    gs.party["wisp"]   = Character(name="Wisp",   max_hp=16, hp=0, ability_modifiers={"dex": 0})
    gs.npcs["snik"]    = NPC(name="Snik", max_hp=12, hp=12)
    tools.dispatch("start_combat", {"combatants": ["aldric", "wisp", "snik"]}, gs)
    assert gs.combat_round == 1

    agent = DMAgent(gs, client=MagicMock())
    ended = agent._maybe_end_combat()

    assert ended is True
    assert gs.combat_round == 0
    assert gs.combat_order == []




def test_player_kills_last_enemy_ends_combat():
    """take_turn whose player action downs the sole hostile: combat_round==0, end_combat in
    trace, post-combat wrap-up used, no '<Name>, what do you do?' prompt appended.
    Scene has exits so the run continues (game_over stays False, post-combat beat fires)."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    rules.seed(0)
    gs = GameState(location="Arena", current_scene="arena")
    gs.scenes = {
        "arena": {"location": "Arena", "exits": {"the vault ahead": "vault"}},
        "vault": {"location": "The Vault", "exits": {}},
    }
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 100})
    gs.npcs["snik"]    = NPC(name="Snik", max_hp=12, hp=12)
    tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)
    assert gs.combat_order[0] == "aldric"

    # Model calls modify_hp(-12) — no dice needed, cleanly downs Snik.
    hp_block = MagicMock()
    hp_block.type = "tool_use"; hp_block.id = "t1"
    hp_block.name = "modify_hp"
    hp_block.input = {"target": "Snik", "amount": -12}
    exec_resp = MagicMock()
    exec_resp.stop_reason = "tool_use"; exec_resp.content = [hp_block]

    done_block = MagicMock(); done_block.type = "text"; done_block.text = ""
    done_resp = MagicMock(); done_resp.stop_reason = "end_turn"; done_resp.content = [done_block]

    narr_block = MagicMock()
    narr_block.type = "text"
    narr_block.text = "Snik crumples. Silence falls. The passage yawns ahead. What do you do?"
    narr_resp = MagicMock(); narr_resp.stop_reason = "end_turn"; narr_resp.content = [narr_block]

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [exec_resp, done_resp, narr_resp]

    agent = DMAgent(gs, client=fake_client)
    narration = agent.take_turn("Aldric finishes Snik")

    assert gs.npcs["snik"].hp == 0

    # Engine ended combat automatically
    assert gs.combat_round == 0
    assert gs.combat_order == []

    # end_combat is in the tool trace
    ec_calls = [c for c in agent.tool_trace if c["name"] == "end_combat"]
    assert len(ec_calls) == 1

    # Post-combat wrap-up prompt used (not the regular narrate)
    third_call_msgs = fake_client.messages.create.call_args_list[2][1]["messages"]
    last_user_content = next(
        m["content"] for m in reversed(third_call_msgs) if m["role"] == "user"
    )
    assert "Combat is over" in str(last_user_content)

    # No combat-turn prompt appended
    assert "Aldric, what do you do?" not in narration
    assert "Snik, what do you do?" not in narration




def test_maybe_end_combat_noop_outside_combat():
    """_maybe_end_combat is idempotent — no-op and no trace entry when not in combat."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    gs = GameState(location="Test")
    gs.party["aldric"] = Character(name="Aldric")
    gs.npcs["snik"] = NPC(name="Snik", max_hp=12, hp=0)  # downed, but not in combat
    agent = DMAgent(gs, client=MagicMock())

    agent._maybe_end_combat()

    assert gs.combat_round == 0       # unchanged
    assert agent.tool_trace == []     # nothing appended




def test_maybe_end_combat_noop_when_enemies_alive():
    """_maybe_end_combat does not end combat when living hostiles remain."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    rules.seed(0)
    gs = GameState(location="Test")
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 0})
    gs.npcs["snik"] = NPC(name="Snik", max_hp=12, hp=12)
    tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)

    agent = DMAgent(gs, client=MagicMock())
    agent._maybe_end_combat()

    assert gs.combat_round == 1       # still in combat
    assert agent.tool_trace == []     # nothing appended




# --- _closing_prompt ---------------------------------------------------------

def _agent_with_trace(gs, trace_calls):
    """Return a DMAgent whose tool_trace is pre-populated with trace_calls."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent
    agent = DMAgent(gs, client=MagicMock())
    agent.tool_trace = trace_calls
    return agent




def test_closing_prompt_names_targets_after_ambiguous():
    """Wisp is active; trace has ambiguous_target with Grik + Narl; prompt addresses
    Wisp and names both candidates — not the generic 'what do you do?'."""
    rules.seed(0)
    gs = _make_combat_state()
    gs.party["wisp"].ability_modifiers["dex"] = 100  # Wisp wins initiative → active
    tools.dispatch("start_combat", {"combatants": ["wisp", "snik"]}, gs)
    assert gs.combat_order[0] == "wisp"

    trace = [{"name": "cast_spell", "input": {"caster": "Wisp"}, "result": {
        "ok": False, "reason": "ambiguous_target", "candidates": ["Grik", "Narl"],
    }}]
    agent = _agent_with_trace(gs, trace)
    prompt = agent._closing_prompt()

    assert prompt is not None
    assert "Wisp" in prompt
    assert "Grik" in prompt
    assert "Narl" in prompt
    assert "what do you do?" not in prompt




def test_closing_prompt_generic_on_normal_turn():
    """Wisp active, no ambiguous_target in trace → 'Wisp, what do you do?'."""
    rules.seed(0)
    gs = _make_combat_state()
    gs.party["wisp"].ability_modifiers["dex"] = 100
    tools.dispatch("start_combat", {"combatants": ["wisp", "snik"]}, gs)
    assert gs.combat_order[0] == "wisp"

    agent = _agent_with_trace(gs, [])
    assert agent._closing_prompt() == "Wisp, what do you do?"




def test_closing_prompt_none_when_not_in_combat():
    """Not in combat (combat_round == 0) → None."""
    gs = _make_combat_state()
    agent = _agent_with_trace(gs, [])
    assert agent._closing_prompt() is None




def test_closing_prompt_generic_in_combat():
    """Active combatant up, no ambiguous_target in trace → generic prompt."""
    rules.seed(0)
    gs = _make_combat_state()
    gs.party["aldric"].ability_modifiers["dex"] = 100
    tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)
    assert gs.combat_order[0] == "aldric"

    agent = _agent_with_trace(gs, [])
    assert agent._closing_prompt() == "Aldric, what do you do?"




def test_closing_prompt_none_outside_combat():
    """Not in combat → None."""
    gs = _make_combat_state()
    agent = _agent_with_trace(gs, [])
    assert agent._closing_prompt() is None




def test_closing_prompt_none_when_active_is_down():
    """Active combatant at 0 HP → None (no prompt for a downed actor)."""
    rules.seed(0)
    gs = _make_combat_state()
    gs.party["aldric"].ability_modifiers["dex"] = 100
    tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)
    gs.party["aldric"].hp = 0

    agent = _agent_with_trace(gs, [])
    assert agent._closing_prompt() is None




def test_closing_prompt_two_candidates():
    """ambiguous_target with 2 candidates → '<Name>, name your target — A or B?'"""
    rules.seed(0)
    gs = _make_combat_state()
    gs.party["aldric"].ability_modifiers["dex"] = 100
    tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)

    trace = [{"name": "attack", "input": {"attacker": "Aldric"}, "result": {
        "ok": False, "reason": "ambiguous_target", "candidates": ["Grik", "Narl"],
    }}]
    agent = _agent_with_trace(gs, trace)
    assert agent._closing_prompt() == "Aldric, name your target — Grik or Narl?"




def test_closing_prompt_three_candidates():
    """ambiguous_target with 3 candidates → '<Name>, name your target — A, B, or C?'"""
    rules.seed(0)
    gs = _make_combat_state()
    gs.party["aldric"].ability_modifiers["dex"] = 100
    tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)

    trace = [{"name": "attack", "input": {"attacker": "Aldric"}, "result": {
        "ok": False, "reason": "ambiguous_target", "candidates": ["Grik", "Narl", "Ugor"],
    }}]
    agent = _agent_with_trace(gs, trace)
    assert agent._closing_prompt() == "Aldric, name your target — Grik, Narl, or Ugor?"




def test_closing_prompt_npc_ambiguous_does_not_pollute_next_player():
    """When an NPC's attack got ambiguous_target this cycle, the next active player
    must receive the generic 'what do you do?' — not the NPC's candidate list."""
    rules.seed(0)
    gs = _make_combat_state()
    gs.party["aldric"].ability_modifiers["dex"] = -100   # goes last
    gs.npcs["snik"].ability_modifiers["dex"] = 100       # goes first
    tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)
    gs.combat_starting = False  # simulate take_turn clearing the barrier before next_turn
    tools.dispatch("next_turn", {}, gs)   # advance pointer to aldric
    assert gs.combat_order[gs.combat_index] == "aldric"

    # Trace contains Snik's ambiguous_target rejection — must not affect Aldric's prompt.
    trace = [{"name": "attack", "input": {"attacker": "Snik"}, "result": {
        "ok": False, "reason": "ambiguous_target", "candidates": ["Grik", "Narl"],
    }}]
    agent = _agent_with_trace(gs, trace)
    assert agent._closing_prompt() == "Aldric, what do you do?"




def test_closing_prompt_ambiguous_only_when_active_is_up():
    """ambiguous_target in trace but active combatant is down → None, not the target prompt."""
    rules.seed(0)
    gs = _make_combat_state()
    gs.party["aldric"].ability_modifiers["dex"] = 100
    tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)
    gs.party["aldric"].hp = 0

    trace = [{"name": "attack", "input": {"attacker": "Aldric"}, "result": {
        "ok": False, "reason": "ambiguous_target", "candidates": ["Grik", "Narl"],
    }}]
    agent = _agent_with_trace(gs, trace)
    assert agent._closing_prompt() is None




def test_closing_prompt_ok_false_non_ambiguous_gives_generic():
    """ok=false with a different reason (e.g. 'no_target') → generic prompt, not target prompt."""
    rules.seed(0)
    gs = _make_combat_state()
    gs.party["aldric"].ability_modifiers["dex"] = 100
    tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)

    trace = [{"name": "attack", "input": {}, "result": {
        "ok": False, "reason": "no_target", "error": "No living hostile targets present.",
    }}]
    agent = _agent_with_trace(gs, trace)
    assert agent._closing_prompt() == "Aldric, what do you do?"




def test_take_turn_emits_ambiguous_target_prompt():
    """End-to-end: when attack returns ambiguous_target, take_turn's output contains
    the candidate-naming prompt rather than the generic 'what do you do?' prompt."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    rules.seed(0)
    gs = GameState(location="Arena")
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 100},
                                   inventory=["mace"], proficiency_bonus=2)
    gs.npcs["grik"] = NPC(name="Grik", max_hp=18, hp=18, hostile=True, ability_modifiers={"dex": 0})
    gs.npcs["narl"] = NPC(name="Narl", max_hp=12, hp=12, hostile=True, ability_modifiers={"dex": -50})

    tools.dispatch("start_combat", {"combatants": ["aldric", "grik", "narl"]}, gs)
    assert gs.combat_order[0] == "aldric"

    # Model calls attack without naming a defender → engine returns ambiguous_target.
    atk_block = MagicMock()
    atk_block.type = "tool_use"; atk_block.id = "t1"
    atk_block.name = "attack"
    atk_block.input = {"attacker": "Aldric", "weapon": "mace"}
    exec_resp = MagicMock()
    exec_resp.stop_reason = "tool_use"; exec_resp.content = [atk_block]

    stop_block = MagicMock(); stop_block.type = "text"; stop_block.text = ""
    stop_resp = MagicMock(); stop_resp.stop_reason = "end_turn"; stop_resp.content = [stop_block]

    narr_block = MagicMock()
    narr_block.type = "text"; narr_block.text = "There are two enemies before you."
    narr_resp = MagicMock(); narr_resp.stop_reason = "end_turn"; narr_resp.content = [narr_block]

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [exec_resp, stop_resp, narr_resp]

    agent = DMAgent(gs, client=fake_client)
    narration = agent.take_turn("Aldric attacks")

    assert "Aldric, name your target" in narration
    assert "Grik" in narration
    assert "Narl" in narration
    assert "Aldric, what do you do?" not in narration




def test_party_wipe_sets_defeat():
    """All PCs at 0 HP with a living hostile: _maybe_end_combat ends combat and sets defeat."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    rules.seed(0)
    gs = GameState(location="Arena")
    gs.party["aldric"] = Character(name="Aldric", max_hp=24, hp=0, ability_modifiers={"dex": 0})
    gs.party["wisp"]   = Character(name="Wisp",   max_hp=16, hp=0, ability_modifiers={"dex": 0})
    gs.npcs["snik"]    = NPC(name="Snik", max_hp=12, hp=12)
    tools.dispatch("start_combat", {"combatants": ["aldric", "wisp", "snik"]}, gs)
    assert gs.combat_round == 1

    agent = DMAgent(gs, client=MagicMock())
    ended = agent._maybe_end_combat()

    assert ended is True
    assert gs.combat_round == 0
    assert gs.game_over is True
    assert gs.game_outcome == "defeat"




def test_combat_victory_no_game_over():
    """All hostiles down, party alive in a scene WITH exits: combat ends, game_over stays
    False — the run may have more scenes to visit."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    rules.seed(0)
    gs = GameState(location="Arena", current_scene="arena")
    gs.scenes = {
        "arena": {"location": "Arena", "exits": {"the vault ahead": "vault"}},
        "vault": {"location": "The Vault", "exits": {}},
    }
    gs.party["aldric"] = Character(name="Aldric", max_hp=24, hp=24, ability_modifiers={"dex": 0})
    gs.npcs["snik"]    = NPC(name="Snik", max_hp=12, hp=0)  # already downed
    tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)

    agent = DMAgent(gs, client=MagicMock())
    ended = agent._maybe_end_combat()

    assert ended is True
    assert gs.combat_round == 0
    assert gs.game_over is False
    assert gs.game_outcome == ""




def test_combat_victory_in_terminal_scene_ends_run():
    """All hostiles down, party alive in a terminal scene (no exits): _maybe_end_combat
    ends combat AND sets game_over=True / game_outcome='victory' — no move_scene needed.
    Covers single-scene scenarios (no scenes dict) and named terminal scenes."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    rules.seed(0)
    gs = GameState(location="Final Chamber")   # no scenes dict — terminal by definition
    gs.party["aldric"] = Character(name="Aldric", max_hp=24, hp=24, ability_modifiers={"dex": 0})
    gs.npcs["snik"]    = NPC(name="Snik", max_hp=12, hp=0)  # already downed
    tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)

    agent = DMAgent(gs, client=MagicMock())
    ended = agent._maybe_end_combat()

    assert ended is True
    assert gs.combat_round == 0
    assert gs.game_over is True
    assert gs.game_outcome == "victory"




def test_game_over_emits_epilogue_no_prompt():
    """take_turn: victory from move_scene → output contains epilogue, no closing prompt."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    gs = GameState(location="Final Chamber", current_scene="final_room")
    gs.scenes = {"final_room": {"location": "Final Chamber", "exits": {}}}
    gs.party["aldric"] = Character(name="Aldric", max_hp=24, hp=24, ability_modifiers={"dex": 0})

    # Call 1 (_execute): model calls move_scene
    ms_block = MagicMock()
    ms_block.type = "tool_use"; ms_block.id = "t1"
    ms_block.name = "move_scene"; ms_block.input = {"scene_key": "onward"}
    exec_resp = MagicMock()
    exec_resp.stop_reason = "tool_use"; exec_resp.content = [ms_block]

    # Call 2 (_execute loop): model sees adventure_complete result, stops
    done_block = MagicMock(); done_block.type = "text"; done_block.text = ""
    done_resp = MagicMock(); done_resp.stop_reason = "end_turn"; done_resp.content = [done_block]

    # Call 3 (_narrate_epilogue): returns victory epilogue
    epi_block = MagicMock()
    epi_block.type = "text"
    epi_block.text = "The party emerges victorious from the depths of the Ashen Barrow."
    epi_resp = MagicMock(); epi_resp.stop_reason = "end_turn"; epi_resp.content = [epi_block]

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [exec_resp, done_resp, epi_resp]

    agent = DMAgent(gs, client=fake_client)
    narration = agent.take_turn("We press onward")

    assert gs.game_over is True
    assert gs.game_outcome == "victory"
    assert "victorious" in narration           # epilogue present
    assert "Aldric, what do you do?" not in narration  # no closing prompt
    assert fake_client.messages.create.call_count == 3  # execute×2, epilogue×1




def test_closing_prompt_youre_up_after_start_combat():
    """When start_combat fires this turn and a PC is active, _closing_prompt uses
    the 'you're up' variant instead of the generic 'what do you do?'."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    rules.seed(0)
    gs = GameState(location="Arena")
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 100})
    gs.npcs["snik"] = NPC(name="Snik", ability_modifiers={"dex": 0})

    sc_result = tools.dispatch("start_combat", {"combatants": ["aldric", "snik"]}, gs)
    assert gs.combat_order[0] == "aldric"

    agent = _agent_with_trace(gs, [
        {"name": "start_combat", "input": {"combatants": ["aldric", "snik"]}, "result": sc_result},
    ])
    prompt = agent._closing_prompt()

    assert prompt is not None
    assert "Aldric" in prompt
    assert "you're up" in prompt




def test_take_turn_start_combat_defers_attack():
    """End-to-end: a player input that calls start_combat AND attack in the same
    tool-use phase has the attack silently blocked (combat_starting barrier).
    The engine then prompts the first PC with the 'you're up' variant."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    rules.seed(0)
    gs = GameState(location="Arena")
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 100},
                                   inventory=["mace"], proficiency_bonus=2)
    gs.npcs["snik"] = NPC(name="Snik", max_hp=12, hp=12, ability_modifiers={"dex": 0})

    # Model returns two tool_use blocks in one hop: start_combat then attack.
    sc_block = MagicMock()
    sc_block.type = "tool_use"; sc_block.id = "t1"
    sc_block.name = "start_combat"; sc_block.input = {"combatants": ["aldric", "snik"]}

    atk_block = MagicMock()
    atk_block.type = "tool_use"; atk_block.id = "t2"
    atk_block.name = "attack"; atk_block.input = {"attacker": "Aldric", "weapon": "mace"}

    exec_resp = MagicMock()
    exec_resp.stop_reason = "tool_use"; exec_resp.content = [sc_block, atk_block]

    done_block = MagicMock(); done_block.type = "text"; done_block.text = ""
    done_resp = MagicMock(); done_resp.stop_reason = "end_turn"; done_resp.content = [done_block]

    narr_block = MagicMock()
    narr_block.type = "text"; narr_block.text = "Torchlight flickers as steel rings out — combat begins."
    narr_resp = MagicMock(); narr_resp.stop_reason = "end_turn"; narr_resp.content = [narr_block]

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [exec_resp, done_resp, narr_resp]

    agent = DMAgent(gs, client=fake_client)
    narration = agent.take_turn("move toward the goblin and attack")

    # Attack was blocked — Snik's HP must be untouched.
    assert gs.npcs["snik"].hp == 12

    # Barrier rejection is in the trace.
    atk_calls = [c for c in agent.tool_trace if c["name"] == "attack"]
    assert len(atk_calls) == 1
    assert atk_calls[0]["result"]["ok"] is False
    assert atk_calls[0]["result"]["reason"] == "combat_starting"

    # action_used is False — the blocked attack did not consume the turn.
    assert gs.action_used is False

    # Closing prompt addresses the first PC with the initiative variant.
    assert "Aldric" in narration
    assert "you're up" in narration




def test_first_pc_not_skipped():
    """When start_combat resolves and the first combatant in initiative order is a PC,
    the NPC loop must halt immediately — next_turn is never called, the combat pointer
    stays on that PC, and the engine emits the 'you're up' closing prompt."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    rules.seed(0)
    gs = GameState(location="Arena")
    gs.party["aldric"] = Character(name="Aldric", ability_modifiers={"dex": 100})  # wins initiative
    gs.npcs["snik"]    = NPC(name="Snik", max_hp=12, hp=12, ability_modifiers={"dex": 0})

    # Model calls start_combat only, then stops — no attack attempt.
    sc_block = MagicMock()
    sc_block.type = "tool_use"; sc_block.id = "t1"
    sc_block.name = "start_combat"; sc_block.input = {"combatants": ["aldric", "snik"]}
    exec_resp = MagicMock()
    exec_resp.stop_reason = "tool_use"; exec_resp.content = [sc_block]

    done_block = MagicMock(); done_block.type = "text"; done_block.text = ""
    done_resp  = MagicMock(); done_resp.stop_reason = "end_turn"; done_resp.content = [done_block]

    narr_block = MagicMock()
    narr_block.type = "text"; narr_block.text = "Swords are drawn — combat begins."
    narr_resp  = MagicMock(); narr_resp.stop_reason = "end_turn"; narr_resp.content = [narr_block]

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [exec_resp, done_resp, narr_resp]

    agent = DMAgent(gs, client=fake_client)
    narration = agent.take_turn("we approach the goblin")

    # Aldric (dex=100) is first in initiative order.
    assert gs.combat_order[0] == "aldric"

    # NPC loop: first combatant is a PC → break on i=0, next_turn never fired.
    next_turns = [c for c in agent.tool_trace if c["name"] == "next_turn"]
    assert next_turns == [], f"next_turn called {len(next_turns)} time(s) — first PC was skipped"

    # Combat pointer unchanged; still round 1.
    assert gs.combat_order[gs.combat_index] == "aldric"
    assert gs.combat_round == 1

    # Engine issues the initiative-announcing closing prompt.
    assert "Aldric" in narration
    assert "you're up" in narration




def test_maybe_end_combat_ungated_terminal_fires_victory():
    """_maybe_end_combat: combat ends in ungated terminal scene -> game_over=True."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    gs = GameState(
        current_scene="end",
        scenes={"end": {"location": "End", "scene": "", "exits": {}}},
    )
    gs.party["aldric"] = Character(name="Aldric", max_hp=20, hp=20, ability_modifiers={"dex": 0})
    gs.npcs["snik"] = NPC(name="Snik", max_hp=10, hp=0)  # already down
    gs.combat_order = ["aldric", "snik"]
    gs.combat_index = 0
    gs.combat_round = 1

    agent = DMAgent(gs, client=MagicMock())
    ended = agent._maybe_end_combat()

    assert ended is True
    assert gs.game_over is True
    assert gs.game_outcome == "victory"
    assert gs.combat_round == 0  # end_combat fired




def test_maybe_end_combat_gated_terminal_ends_combat_not_game():
    """_maybe_end_combat: combat ends in gated terminal scene -> combat cleared, game_over False."""
    from unittest.mock import MagicMock
    from src.dm_agent import DMAgent

    gs = GameState(
        current_scene="final_chamber",
        scenes={
            "final_chamber": {
                "location": "Final Chamber",
                "scene": "",
                "exits": {},
                "exit_requires": "iron_door_open",
            },
        },
    )
    gs.party["aldric"] = Character(name="Aldric", max_hp=20, hp=20, ability_modifiers={"dex": 0})
    gs.npcs["snik"] = NPC(name="Snik", max_hp=10, hp=0)  # already down
    gs.combat_order = ["aldric", "snik"]
    gs.combat_index = 0
    gs.combat_round = 1

    agent = DMAgent(gs, client=MagicMock())
    ended = agent._maybe_end_combat()

    assert ended is True
    assert gs.combat_round == 0   # combat ended
    assert gs.game_over is False  # victory deferred — party must open the gated exit
