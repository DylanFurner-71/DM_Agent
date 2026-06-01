"""The DM agent: a tool-use loop around the Anthropic Messages API.

Narration is folded into the tool loop rather than spent on a separate call:
  - Out of combat, the tool loop's terminating turn IS the narration. The model
    resolves the action with tools, then its final (non-tool) message is the
    in-world prose — the generation that used to be thrown away now does the work.
  - In combat, the player's action is resolved tool-only, the engine then runs the
    following NPC beats, and ONE unified narration call (_narrate_turn) covers the
    player's action plus every NPC beat in order, so none are reordered or skipped.

Combat-over and end-of-run still use dedicated calls (_narrate_combat_over,
_narrate_epilogue) because their prose is shaped differently and is resolved after
the action. take_turn appends an engine-sourced closing prompt for the next player.

The leak guards (_extract_narration / _sanitize_narration) are load-bearing here:
because prose is now produced with tool JSON in immediate context, they are the
primary defense against a state dump reaching the player. See DECISIONS.md.
"""

from __future__ import annotations

import json
import logging
import os
import time

from anthropic import Anthropic

from . import tools

# NOTE: confirm the current model string at https://docs.anthropic.com/en/docs/about-claude/models
MODEL = "claude-sonnet-4-6"
MAX_TOOL_HOPS = 12      # safety cap on tool calls per _execute call
NARRATION_WINDOW = 4    # past (player_input, narration) pairs kept in model context

SYSTEM_PROMPT = """\
You are the Dungeon Master for a single-session tabletop RPG. You narrate vividly \
and concisely, voice NPCs, and keep the story moving.

HARD RULES — these are not optional:
- You do NOT know any game numbers from memory. For every dice roll, attack, spell, \
HP change, or rules question, you MUST call the matching tool and use its result.
- Never invent a dice result or override a tool's outcome. If `cast_spell` returns \
ok=false because the caster is out of slots, the spell FAILS — narrate the fizzle, \
do not let it succeed anyway.
- When an action tool returns ok=false because the action itself is invalid — attacking \
with a weapon the attacker doesn't own, casting a spell the caster doesn't know, or \
casting with no slot of that level — SURFACE the failure in your narration in plain \
language and re-prompt that same character, e.g. "Aldric has no dagger — he's carrying a \
mace. What does Aldric do?" The engine keeps that character's turn alive; do NOT treat \
this as a turn change. Never silently substitute a different weapon or spell, and never \
fabricate a success. (This is distinct from a turn-guard rejection — "it is not X's turn" \
— which is the engine enforcing order and does advance the pointer.)
- When calling `attack` or `cast_spell`, supply `defender`/`target` only if the player's \
input explicitly names a specific target. If the player says "attack" or "cast chromatic \
orb" without naming who, omit the argument entirely so the engine can auto-select or \
return ambiguous_target as appropriate. Never pre-fill a target the player did not name.
- When an action tool returns ok=false with reason "ambiguous_target", the result includes \
a candidates list — ask the player which target they meant by naming every candidate, \
and re-prompt that same character without advancing the turn. Never choose a target on \
the player's behalf.
- Any roll that changes tracked state (HP, slots, or conditions) MUST use the tool that \
rolls AND applies atomically. Weapon damage → `attack`. Spell damage → `cast_spell`. \
Trap, hazard, or potion dice → `apply_dice`. `roll_dice` is for fiction-only randomness \
(encounter table, NPC flee direction, coins in a chest) and must NEVER feed `modify_hp`. \
`modify_hp` accepts flat, known amounts only (e.g. "the mechanism deals exactly 10").
- When you need exact current numbers (HP, remaining slots, who's present), call \
`get_state` rather than guessing.
- Scene geography is fixed. When `exits` appears in the state snapshot, those are the \
ONLY paths that exist — narrate only declared exits, never invent a passage, door, fork, \
or room that isn't listed. When the player moves, match their intent to a declared exit \
and call `move_scene` with that exit's scene_key. A scene whose exits map is empty (or \
absent) is a dead end; tell the player there is nowhere further to go. When combat ends \
in a terminal scene the engine closes the run and requests a closing epilogue — write it \
and stop; never call `move_scene` to fabricate an exit that was not listed.
- GATED WAYS — some exits and endings require a quest flag (a key, a password, a met \
condition). The state marks which ways are gated and the flag each needs. If the party \
uses a gated way without the flag, move_scene returns ok=false reason 'locked' — narrate \
the way as barred using the denied text, do NOT treat it as a transition, and never \
fabricate passage. When the party has the flag, the way opens. In a terminal scene whose \
ending is gated, winning the final fight does NOT end the run: narrate the aftermath, then \
the party must open the gated exit (e.g. the iron door) — call move_scene when they do, \
and the engine grants victory if the flag is set.
- Some exits are sealed by a spoken word, shown as `answer_required`. When the player \
speaks a word, name, or phrase at such a door, relay EXACTLY what they said as \
move_scene's `answer` — do not translate it, complete it, correct it, or supply a word yourself.
- Never volunteer or hint a password the party hasn't earned in the fiction. The player \
must have learned it (e.g. from something they found) and chosen to speak it. If they \
speak the wrong word or none, the door stays shut — narrate the refusal from the engine's \
`denied` text; never open it for them.
- If the player heads for an `answer_required` exit without speaking anything, it's \
locked — prompt them for what they say or do; do not call move_scene with a guessed answer.
- NEVER INVENT A PASSWORD, ANSWER, OR PUZZLE WORD. A spoken word that opens a way exists \
only where the authored fiction put it — the scene text, a clue the party found. If no such \
word has appeared, you do not know one, and you must not coin a plausible-sounding one (a \
made-up word will fail the engine's gate and strand the player). When the player reads, \
re-reads, or examines something whose wording already appeared in the scene — a journal, an \
inscription, a clue carrying a name or password — restate that authored text EXACTLY as \
written; do not paraphrase the key word, embellish it, or substitute a different one. The \
literal word on a re-read must match the word the player first saw.
- When the engine requests a closing epilogue ("The adventure is over — the party has \
prevailed" or "…has fallen"), write the single paragraph asked for. The session ends \
there — no prompts, no further turns, no improvised continuation.
- Stay in the fiction. Never expose raw tool JSON, internal reasoning, process notes, \
or meta-commentary.
- QUEST FLAGS — record durable facts, not actions. The line is action vs. discovery: what the party \
DOES (approaching, attacking, moving, searching, reading, casting) is never a flag, but a FACT they \
discover, a CHANGE they cause, or a COMMITMENT they make usually is. When you reveal a clue, warning, \
name, password, or secret about what lies ahead, record the fact learned — not the act of learning it. \
Likewise a door unlocked, a lever thrown, a promise made, an NPC spared or won over. Test: would a \
later turn contradict itself, or the player be surprised, if you forgot this? If yes, flag it. If \
forgetting it costs nothing because it was just an action, don't. \
Do NOT flag routine actions or anything already in state — 'approached_snik', 'searched_room', HP, \
who's down, location, inventory, an NPC's hostile field. If a fact has a dedicated home it is not a \
flag, and never store mechanical values. Read quest_flags each turn and stay consistent with them.
- SAVING THROWS — when an effect happens TO a character that they resist (a trap's \
dex save, poison's con save, a fear effect's wis save, a shove's str save), call \
`saving_throw` with the ability and DC — NOT `skill_check`, which is a proactive action \
the character chooses to take. A save is REACTIVE: it is not an action, costs no turn, \
and may be rolled for any affected character on anyone's turn (e.g. the whole party \
saves at once against a trap). The engine owns the roll; never decide a save's outcome \
yourself. On a FAILED save, apply the consequence with the matching tool — `apply_dice` \
for dice damage, `modify_hp` for a flat amount — and narrate it; on a success, narrate \
the effect resisted or reduced. (Distinct from the death-save cycle, which the engine \
rolls automatically for dying PCs.)
- A PC at 0 HP is unconscious and dying; they cannot act. The engine rolls their death \
saves automatically at their turn — never roll one yourself, never have a dying PC \
attack/cast/move, never prompt them. Healing a dying or stable PC brings them back and \
the engine resets their saves; narrate the revival. When the engine reports a death save, \
narrate its given outcome — do not change it.
- Loot is author-placed. You may only grant items via `take_item`, and only items in the \
current scene's loot list — never invent treasure. Reveal loot through exploration (a search, \
opening a chest, a successful check), not by announcing the list.
- REVEALS — never gate authored content behind a check you invent. Loot present in the state and \
facts written into a scene are found by the player's interaction — searching, reading, examining — \
NOT by a skill_check or roll_dice you make up. Do not roll to decide whether the party finds placed \
loot or reads a legible clue; reveal it directly. Roll only when an item or secret is explicitly \
marked hidden. After revealing a clue, record the fact learned with set_quest_flag.
- Consumables have fixed engine effects. Use `use_item` to spend one; never narrate a potion \
healing a specific amount yourself — the tool rolls and applies it.
- Drinking a potion or using an item in combat costs that character's action, exactly like an \
attack. Only the active combatant may do it, on their turn. They may instead administer the item \
to a party ally by passing `target` — including a downed, unconscious ally (a healing potion \
revives them); the action belongs to the active giver, not the recipient.
- SOCIAL — talking a foe down. When the party tries to persuade or intimidate a HOSTILE NPC into \
standing down, call influence_npc with the approach; the engine rolls against that NPC's authored \
difficulty and applies the result. Never decide a negotiation's outcome yourself or flip a \
disposition in narration — only influence_npc (or an attack) changes it. A success makes the NPC \
non-hostile: it stands down, and if it was the last hostile the engine ends combat. A non-hostile \
NPC takes no hostile action on its turn — narrate it standing aside. Each NPC can be swayed once; \
a failed attempt cannot be retried. Out of combat, a FAILED parley triggers an automatic fight — \
the engine rolls initiative for all present fighters and merges the result into influence_npc: \
announce the order from combat_order and stop, exactly as you would after start_combat. Do NOT \
call start_combat yourself. In combat a failed attempt simply ends the character's turn. \
Attacking a non-hostile NPC makes it hostile again.
- COMPANIONS — recruiting an ally. Once an NPC is non-hostile, the party may invite it to join. \
Call recruit_npc with the NPC's name (between fights, not mid-combat). A companion follows the \
party across scenes automatically and, when you include it in start_combat, fights hostiles on \
the party's side — the engine resolves its attacks for you, narrate the beat as you would a foe's. \
Only recruit when the player actually asks the ally to come along; never auto-recruit. A companion \
does not replace a party member — a full party wipe is still a defeat even if it survives.

STEALTH — getting the drop. Before a fight starts, when the party wants to sneak up on the \
enemy, call attempt_ambush with NO arguments. It is a group stealth check: the engine rolls \
Dex for every conscious party member against the highest alertness DC among the living \
hostiles, and the party succeeds only if every member beats the bar (weakest-link). Never \
decide the outcome yourself. This tool does NOT start combat — its result is only \
ok/success/bar/rolls/hostiles. If the attempt is valid (ok=true), call start_combat next; \
on success start_combat will mark the hostiles surprised so they lose their first turn. \
Do NOT list the initiative order yourself — the engine prints it. Narrate the moment and stop. \
On SUCCESS (success=true) narrate the party getting the drop; on FAILURE (success=false) no \
surprise is granted — narrate the enemies noticing the approach, then the fair fight. \
attempt_ambush is rejected (ok=false) when combat is already in progress ('in_combat'), an \
attempt was already made this scene ('already_attempted'), there are no living hostiles \
('no_target'), or a foe cannot be caught off guard ('cannot_ambush') — narrate that last one \
as already watching. Ambush is only possible before combat. \
A surprised enemy takes no action on its first turn; the engine enforces this — never have a \
surprised enemy act in round 1. If the party would rather avoid the enemies entirely, you may \
narrate them slipping past and move on without combat — that needs no tool.

RESOLVING AN ACTION — every action prompt is a [Tool-use phase]: call the tools that \
resolve the action. The prompt tells you how to finish, in one of two styles:
- "write your FINAL message as narration": once EVERY tool has resolved, your final \
message is 1-3 sentences of in-world prose describing what just happened — damage dealt, \
spell effect, hit or miss, movement. Emit prose ONLY in that final message: never in the \
same response as a tool call, never before the tools are done.
- "write no prose": emit tool calls only; the turn's narration is requested separately \
afterward (this is how combat turns work — the engine narrates the whole exchange at once).
Either way, during the tool phase output no tool JSON, no state dumps, no "[Current state]", \
no prompts, no "what do you do", no turn banners, no meta-commentary. When a single action \
implies multiple INDEPENDENT tool calls (e.g. taking several items plus setting a quest flag), \
emit them as parallel tool_use blocks in one response instead of one tool per hop. Keep \
dependent/sequential calls (start_combat before attack, etc.) sequential, and narrate (when \
asked) only after the last one resolves.

COMBAT FLOW:
1. STARTING: Before the first attack or offensive spell, call `start_combat` with every \
participant. Never call `attack` or `cast_spell` offensively before `start_combat`. \
`start_combat` is the TERMINAL call for the input that triggers it — once you call it, \
do NOT also call `attack`, `cast_spell`, `skill_check`, or any other action tool in \
the same [Tool-use phase]; the engine will deny it with reason "combat_starting". \
Do NOT list the initiative order yourself — the engine prints it. Narrate the outbreak of \
combat and stop; the engine runs any leading NPC turns and then prompts the first PC.
2. TURN ORDER: `next_turn` is not available to you — the engine advances the pointer. \
The preamble shows "[Combat: Round N — Name's turn]" so you always know who is active. \
Only that combatant may act; the tools enforce this and return ok=false if you try to \
act for someone else. A turn-guard ok=false is a HARD STOP: call no further tools in \
this [Tool-use phase] — the engine will advance the pointer and prompt the correct player. \
If start_combat reports an active combatant different from the one the player named, \
stop immediately without attempting the named action.
3. NPC TURNS: In the tool-use phase, decide the NPC's action (hostile NPCs attack; \
frightened ones flee) and execute it with `attack`, `cast_spell`, `skill_check`, etc.
4. ENDING: The engine ends combat automatically when all enemies or all party members \
reach 0 HP — do NOT call `get_state` to check HP counts or call `end_combat` for \
defeat. `end_combat` is only for narrative endings where no one reaches 0 HP: enemies \
flee, surrender, or parley — call it yourself only in those cases.
5. POST-COMBAT BEAT: When `end_combat` fires, the engine requests a two-paragraph \
closing beat — (1) the finishing blow and its immediate aftermath; (2) brief stock of \
the party (wounds, spent slots, the body, the sudden silence), then re-orient to the \
surroundings (exits, what lies ahead, any points of interest) and close with ONE open \
exploration prompt to the whole party, e.g. "The passage yawns ahead. What do you do?" \
Never use a "<Name>, what do you do?" combat-turn prompt here.

Keep the player's agency central: present situations, then react to what they choose.
"""

_SYSTEM = [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]
_TOOLS_CACHED = [*tools.TOOLS[:-1], {**tools.TOOLS[-1], "cache_control": {"type": "ephemeral"}}]

_DUMP_SENTINELS = ('[Current state]', '[Engine:', '{"location"', '"party":')


def _sanitize_narration(text: str) -> str:
    """Guard against state-dump leaks reaching persistent storage.

    Checks every line for known internal preambles. If one is found, splits on
    the first blank paragraph boundary and returns only what follows. If nothing
    survives, returns "". Logs a warning whenever text is trimmed.
    """
    if not any(line.lstrip().startswith(s) for line in text.splitlines() for s in _DUMP_SENTINELS):
        return text
    tail = text.split("\n\n", 1)[1].strip() if "\n\n" in text else ""
    logging.warning("_sanitize_narration: trimmed likely state-dump from narration: %.80s", text)
    return tail


def _screen_narration_text(text: str) -> str:
    """Suppress an assembled narration string that looks like an internal state dump.

    Returns "" (and logs a warning) when the text reads as a leaked state/JSON blob
    rather than prose. The leading-bracket-plus-brace shape is the observed leak.
    """
    text = text.strip()
    if text.startswith("[") and "{" in text:
        logging.warning("_screen_narration_text: suppressed likely state-dump leak: %.80s", text)
        return ""
    return text


def _extract_narration(content: list) -> str:
    """Join text blocks from an API response content list and return the narration string.

    Returns "" (and logs a warning) when the text looks like an internal state dump
    or engine preamble that slipped through the tool-use phase scrub.
    """
    text = "".join(b.text for b in content if b.type == "text")
    return _screen_narration_text(text)


class _NarrationGate:
    """Streams narration deltas to a sink, holding the leading text until it is known
    not to be a state dump.

    Leak sentinels appear at the START of a leaked narration (the model prepends a
    `[Current state]…{…}` blob before any prose). So we buffer the opening until we
    can decide: text that does not begin with `[`/`{`/`"party"` is plain prose and is
    released immediately, then passed through live; bracket/brace-leading text is held
    until a paragraph boundary lets `_screen_narration_text` + `_sanitize_narration`
    decide what (if anything) survives — keeping on-screen output equal to what is
    stored. The realistic leak (dump-first) never reaches the sink.
    """

    def __init__(self, sink):
        self.sink = sink
        self.buffer = ""
        self.emitting = False

    def feed(self, delta: str) -> None:
        if self.emitting:
            self.sink(delta)
            return
        self.buffer += delta
        stripped = self.buffer.lstrip()
        looks_risky = stripped[:1] in ("[", "{") or stripped.startswith('"party"')
        if not looks_risky:
            # Plain prose — release the held opening and switch to live pass-through.
            self._release(self.buffer)
            self.buffer = ""
            self.emitting = True
        elif "\n\n" in self.buffer:
            # A paragraph closed; let the screens trim any leading dump, emit the rest.
            safe = _sanitize_narration(_screen_narration_text(self.buffer))
            if safe:
                self._release(safe)
                self.buffer = ""
                self.emitting = True
            # else: still all-dump so far — keep buffering.

    def close(self) -> None:
        """Flush whatever remains once the stream ends."""
        if self.emitting:
            return
        safe = _sanitize_narration(_screen_narration_text(self.buffer))
        if safe:
            self._release(safe)

    def _release(self, text: str) -> None:
        if text:
            self.sink(text)


def _usage_dict(usage) -> dict:
    d: dict = {"input": usage.input_tokens, "output": usage.output_tokens}
    cr = getattr(usage, "cache_read_input_tokens", 0) or 0
    cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
    if cr:
        d["cache_read"] = cr
    if cw:
        d["cache_write"] = cw
    return d



def _record_turn(game_state, player_text: str, dm_text: str) -> None:
    """Append one turn's narrative to the transcript in chronological order.

    Appends two entries: the player's input then the DM's narration. This
    function is model-free and has no side effects beyond mutating transcript.
    """
    game_state.transcript.append({"kind": "player", "text": player_text})
    game_state.transcript.append({"kind": "dm", "text": dm_text})


def _context_from_transcript(transcript: list[dict], bound: int) -> list[dict]:
    """Return the last `bound` transcript entries as API message dicts.

    Maps kind to role (player→user, dm→assistant). When the transcript has
    fewer than `bound` entries, all are returned. The caller is responsible
    for choosing `bound` to match the live-loop window (typically NARRATION_WINDOW*2).
    """
    tail = transcript[-bound:] if bound > 0 else []
    return [
        {
            "role": "user" if entry["kind"] == "player" else "assistant",
            "content": entry["text"],
        }
        for entry in tail
    ]


class DMAgent:
    def __init__(self, state, client: Anthropic | None = None, model: str = MODEL):
        self.state = state
        self.client = client or Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        self.model = model
        # Optional sink for live narration deltas. When set, the dedicated narration
        # calls stream their prose to it (behind a leak gate) for perceived latency;
        # when None, narration is produced with the buffered create() path unchanged.
        self.on_narration_delta = None
        self.messages: list[dict] = []
        self.tool_trace: list[dict] = []  # tool calls from the last turn
        self.api_stats: list[dict] = []   # per-API-call timing/usage for the current turn
        self.full_trace: list[dict] = []  # cumulative [{turn, calls, api_calls}] across all turns; read by /trace
        self.narration_history: list[tuple[str, str]] = []  # rolling (player_input, narration) window
        # Seed the rolling window from the transcript tail so a resumed session
        # starts with the same recent-narrative context as a live one.
        if state.transcript:
            msgs = _context_from_transcript(state.transcript, NARRATION_WINDOW * 2)
            for i in range(0, len(msgs), 2):
                if i + 1 < len(msgs) and msgs[i]["role"] == "user" and msgs[i + 1]["role"] == "assistant":
                    self.narration_history.append((msgs[i]["content"], msgs[i + 1]["content"]))

    def _state_snapshot(self) -> str:
        """Compact JSON of current game state for injection into each turn's prompt.

        Reads directly from self.state so it always reflects the latest HP, slots,
        combat order, and scene — not whatever was true N turns ago.
        """
        s = self.state
        party = {}
        for c in s.party.values():
            entry: dict = {"hp": f"{c.hp}/{c.max_hp}"}
            if c.spell_slots:
                entry["spell_slots"] = c.spell_slots
            if c.conditions:
                entry["conditions"] = c.conditions
            if c.inventory:
                entry["inventory"] = c.inventory
            if c.spells:
                entry["spells"] = c.spells
            party[c.name] = entry

        snap: dict = {
            "location": s.location,
            "party": party,
        }
        if s.npcs:
            def _npc_entry(n):
                entry = {"hp": f"{n.hp}/{n.max_hp}", "hostile": n.hostile}
                if n.social_attempted:
                    entry["social_attempted"] = True
                if n.surprised:
                    entry["surprised"] = True
                if getattr(n, "companion", False):
                    entry["companion"] = True
                return entry
            snap["npcs"] = {n.name: _npc_entry(n) for n in s.npcs.values()}
        if s.current_scene:
            snap["current_scene"] = s.current_scene
            if s.scenes:
                scene_data = s.scenes.get(s.current_scene, {})
                exits = scene_data.get("exits", {})
                if exits:
                    snap["exits"] = tools._exits_for_model(exits)
                loot = scene_data.get("loot", [])
                if loot:
                    snap["loot"] = loot
                exit_req = scene_data.get("exit_requires")
                if exit_req:
                    snap["exit_requires"] = exit_req
                reinf = tools._available_reinforcements(scene_data, s.quest_flags)
                if reinf:
                    snap["reinforcements"] = reinf
        if s.quest_flags:
            # Redact string-valued flags (e.g. answer-gate passwords) the same way
            # get_state does — this snapshot is injected into model context every
            # turn, so a raw string secret here would leak continuously.
            snap["quest_flags"] = tools._redact_quest_flags(s.quest_flags)
        if s.combat_round > 0:
            all_actors = {**s.party, **s.npcs}
            snap["combat"] = {
                "round": s.combat_round,
                "turn_order": [all_actors[k].name for k in s.combat_order if k in all_actors],
                "active": all_actors[s.combat_order[s.combat_index]].name,
            }
        return json.dumps(snap, indent=2)

    def _build_turn_context(self) -> list[dict]:
        """Fresh bounded message list for the start of a new turn.

        Contains only the last NARRATION_WINDOW (player_input, narration) pairs —
        no tool_use/tool_result blocks from prior turns, no stale state. The state
        snapshot and current player input are added by take_turn via _execute.
        """
        messages: list[dict] = []
        for player_inp, narration in self.narration_history[-NARRATION_WINDOW:]:
            messages.append({"role": "user", "content": f"Player: {player_inp}"})
            messages.append({"role": "assistant", "content": narration})
        return messages

    def _execute(self, prompt: str, capture_narration: bool = False,
                 stop_when_action_used: bool = False) -> str:
        """Tool-use phase for one action. Runs the loop; state mutates.

        When capture_narration is True, the model's terminating (non-tool) response
        IS the in-world narration of the action: it is leak-screened by
        _extract_narration and returned. This folds the old dedicated narration call
        into the tool loop's terminating turn — the generation that used to be thrown
        away now does the work. When False (combat turns, whose prose is produced
        later by the unified turn narration, and NPC fallback turns), the terminating
        text is scrubbed so premature prose can't leak forward, and "" is returned.
        """
        self.messages.append({"role": "user", "content": prompt})
        narration = ""
        for _ in range(MAX_TOOL_HOPS):
            _t0 = time.monotonic()
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=_SYSTEM,
                tools=_TOOLS_CACHED,
                messages=self.messages,
            )
            _elapsed = time.monotonic() - _t0
            terminal = resp.stop_reason != "tool_use"
            phase = "narrating" if (terminal and capture_narration) else "thinking"
            self.api_stats.append({"phase": phase, "elapsed": round(_elapsed, 2), "usage": _usage_dict(resp.usage)})
            self.messages.append({"role": "assistant", "content": resp.content})
            if terminal:
                if capture_narration:
                    narration = _extract_narration(resp.content)
                break
            tool_results = []
            for block in resp.content:
                if block.type == "tool_use":
                    result = tools.dispatch(block.name, block.input, self.state)
                    self.tool_trace.append({"name": block.name, "input": block.input, "result": result})
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": _json(result),
                    })
            self.messages.append({"role": "user", "content": tool_results})
            # Combat player-turn fast path: once the actor's action is spent, stop —
            # don't pay for a terminal model turn whose prose is scrubbed anyway, and
            # don't let the model drive next_turn / NPC turns the engine resolves for
            # free in take_turn. (This also keeps end_combat in engine hands so the
            # victory check in _maybe_end_combat runs while combat_round is still > 0.)
            if stop_when_action_used and self.state.action_used:
                break

        # When NOT capturing narration, scrub text the model emitted in its
        # terminating turn — premature prose must not leak into the later unified
        # narration. When capturing, that terminating text IS the narration (already
        # leak-screened) and is kept in the conversation for continuity.
        if not capture_narration:
            for i in range(len(self.messages) - 1, -1, -1):
                msg = self.messages[i]
                if msg["role"] == "assistant":
                    if isinstance(msg["content"], list):
                        msg["content"] = [
                            b for b in msg["content"]
                            if not (hasattr(b, "type") and b.type == "text")
                        ]
                        if not msg["content"]:
                            self.messages.pop(i)
                    break
        return narration

    def _narration_call(self, max_tokens: int, phase: str) -> str:
        """Make a text-only narration call (no tools) over the current self.messages.

        When on_narration_delta is set, the prose is streamed to it live (behind a
        _NarrationGate that holds the opening until a leading state-dump can be ruled
        out); otherwise the buffered create() path is used unchanged so the no-API
        tests and library callers see identical behavior. Records stats, appends the
        assistant turn, and returns the leak-screened narration string either way.
        """
        _t0 = time.monotonic()
        if self.on_narration_delta is None:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=_SYSTEM,
                messages=self.messages,
            )
            self.api_stats.append({"phase": phase, "elapsed": round(time.monotonic() - _t0, 2), "usage": _usage_dict(resp.usage)})
            self.messages.append({"role": "assistant", "content": resp.content})
            return _extract_narration(resp.content)

        gate = _NarrationGate(self.on_narration_delta)
        with self.client.messages.stream(
            model=self.model,
            max_tokens=max_tokens,
            system=_SYSTEM,
            messages=self.messages,
        ) as stream:
            for delta in stream.text_stream:
                gate.feed(delta)
            final = stream.get_final_message()
        gate.close()
        self.api_stats.append({"phase": phase, "elapsed": round(time.monotonic() - _t0, 2), "usage": _usage_dict(final.usage)})
        self.messages.append({"role": "assistant", "content": final.content})
        return _extract_narration(final.content)

    def _emit(self, text: str) -> None:
        """Push a pre-computed, already-leak-screened narration chunk to the live sink.

        Used for the segments that are NOT produced by a streaming narration call —
        the out-of-combat captured narration and the engine's closing prompt — so the
        terminal receives the whole turn in order from one source.
        """
        if text and self.on_narration_delta is not None:
            self.on_narration_delta(text)

    def _narrate_combat_over(self) -> str:
        """Post-combat narration: finishing blow, party stock, exploration prompt."""
        party_summary = "; ".join(
            f"{c.name} HP {c.hp}/{c.max_hp}"
            + (f" [slots: {', '.join(f'L{l}:{n}' for l, n in sorted(c.spell_slots.items()))}]"
               if c.spell_slots else "")
            for c in self.state.party.values()
        )
        prompt = (
            "Combat is over — the fight has just ended. "
            "Write two short paragraphs:\n"
            "1. The finishing blow and its immediate aftermath.\n"
            f"2. Brief stock of the party ({party_summary}) — wounds, spent resources, "
            "the body, the sudden silence. Re-orient to the surroundings: exits, what "
            "lies ahead, any points of interest. Close with one open exploration prompt "
            "to the whole party, e.g. 'The passage yawns ahead into the dark. What do "
            "you do?' — not a combat-turn prompt."
        )
        self.messages.append({"role": "user", "content": prompt})
        return self._narration_call(max_tokens=400, phase="narrating_combat_close")

    def _narrate_turn(self, player_input: str, combat_beats: list[dict]) -> str:
        """Single narration call for a combat turn: the player's action AND every
        NPC beat that followed, in order. Folds what used to be two calls (a player
        narration plus a separate NPC-beat batch) into one.

        The player's tool results and each NPC's [Engine: …] outcome are already in
        self.messages; the prompt just enumerates the beats so none are skipped,
        merged, or reordered. NPC-action beats need only name + round; death-save
        beats carry the engine outcome as ground truth so the model dramatizes it
        without inventing the roll.
        """
        lines = [
            f'1. The party\'s action — "{player_input}". Narrate the actual outcome '
            f"shown in the tool results above (hit or miss, damage, spell effect, movement)."
        ]
        for i, beat in enumerate(combat_beats):
            n = i + 2
            if beat["kind"] == "npc_action":
                lines.append(f"{n}. {beat['name']} (Round {beat['round']})")
            else:  # death_save
                lines.append(
                    f"{n}. {beat['name']} death save (Round {beat['round']}): {beat['outcome']}"
                )
        action_list = "\n".join(lines)
        total = len(lines)
        prompt = (
            f"Narrate each of the following {total} beat(s) in order. "
            f"Write 1–3 sentences of in-world prose per beat — exactly one beat per entry, "
            f"none skipped, none merged, no reordering. "
            f"For death save beats, dramatize the given outcome — do not change or invent the roll. "
            f"No tool calls. No prompts. No meta-commentary. No state dumps.\n\n"
            f"{action_list}"
        )
        self.messages.append({"role": "user", "content": prompt})
        return self._narration_call(max_tokens=min(256 * total, 1024), phase=f"narrating_turn_{total}beats")

    def _adjudicate_combat_outcome(self) -> None:
        """Decide victory/defeat from the post-combat board and set game_over/outcome.

        Party wipe → defeat (takes precedence over a mutual kill). Otherwise, all
        hostiles cleared in an ungated terminal scene → victory; a gated terminal
        defers victory until the flagged exit is opened. Idempotent and independent
        of combat_round, so it produces the same verdict whether the *engine* ended
        combat (via _maybe_end_combat) or the *model* did (a narrative end_combat for
        surrender/flee, or a stray call) — the latter zeroes combat_round and would
        otherwise skip the verdict, which is the bug that left the epilogue unfired.
        """
        s = self.state
        if s.game_over:
            return
        if not any(not c.is_down for c in s.party.values()):
            s.game_over = True
            s.game_outcome = "defeat"
            return
        if any(n.hostile and not n.is_down for n in s.npcs.values()):
            return  # hostiles remain — no terminal verdict to declare
        scene = s.scenes.get(s.current_scene, {}) if s.scenes else {}
        if not scene.get("exits", {}) and not scene.get("exit_requires"):
            s.game_over = True
            s.game_outcome = "victory"

    def _maybe_end_combat(self) -> bool:
        """End combat automatically when one side is entirely down.

        Fires end_combat via dispatch so take_turn routes to the post-combat
        wrap-up narration. Idempotent — no-op when not in combat.
        Returns True if combat was ended, False otherwise.
        """
        if self.state.combat_round == 0:
            return False
        living_hostiles = any(n.hostile and not n.is_down for n in self.state.npcs.values())
        living_party = any(not c.is_down for c in self.state.party.values())
        if not living_hostiles or not living_party:
            result = tools.dispatch("end_combat", {}, self.state)
            self.tool_trace.append({"name": "end_combat", "input": {}, "result": result})
            self._adjudicate_combat_outcome()
            return True
        return False

    def _narrate_epilogue(self, outcome: str) -> str:
        """Single closing paragraph ending the adventure — victory or defeat."""
        if outcome == "victory":
            prompt = (
                "The adventure is over — the party has prevailed. "
                "Write a single triumphant closing paragraph: the final image, the aftermath, "
                "what was won. No tool calls. No prompts. No meta-commentary."
            )
        else:
            prompt = (
                "The adventure is over — the party has fallen. "
                "Write a single somber closing paragraph: the final image, the silence that "
                "follows, what was lost. No tool calls. No prompts. No meta-commentary."
            )
        self.messages.append({"role": "user", "content": prompt})
        return self._narration_call(max_tokens=300, phase="narrating_epilogue")

    def _initiative_banner(self) -> str | None:
        """One-time initiative readout, emitted when combat starts this turn.

        A deterministic, engine-sourced line — like the closing prompt — so the order
        is always shown to the player on a plain start_combat exactly as it is after an
        ambush, rather than depending on the model to recite it. Built from the
        start_combat result captured in this turn's tool_trace (its confirmed order and
        surprised set), so it is robust to later combat_index/surprise mutation. Returns
        None when combat did not start this turn.
        """
        sc = next(
            (c for c in self.tool_trace
             if c["name"] == "start_combat" and c["result"].get("ok")),
            None,
        )
        if not sc:
            return None
        order_keys = sc["result"].get("combat_order", [])
        if not order_keys:
            return None
        all_actors = {**self.state.party, **self.state.npcs}
        surprised = {n.lower() for n in sc["result"].get("surprised", [])}
        parts = [
            f"{(all_actors[k].name if k in all_actors else k)}"
            + (" *(surprised)*" if k.lower() in surprised else "")
            for k in order_keys
        ]
        return "**Initiative order:** " + " → ".join(parts)

    def _closing_prompt(self) -> str | None:
        """Engine-sourced closing prompt for the active combatant.

        Returns None when not in combat or the active combatant is down.
        If this turn's tool_trace contains an ambiguous_target rejection, names
        the candidates: "<Name>, name your target — A or B?" (2 candidates) or
        "<Name>, name your target — A, B, or C?" (3+). Otherwise returns the
        generic "<Name>, what do you do?".
        """
        if not self.state.combat_order or self.state.combat_round == 0:
            return None
        all_actors = {**self.state.party, **self.state.npcs}
        active_key = self.state.combat_order[self.state.combat_index]
        actor = all_actors.get(active_key)
        if not actor or actor.is_down:
            return None
        name = actor.name
        name_lower = name.lower()
        for call in self.tool_trace:
            result = call.get("result", {})
            inp = call.get("input", {})
            if not result.get("ok") and result.get("reason") == "ambiguous_target":
                # Only surface a disambiguation prompt for the *current* active actor's
                # rejection — not for an NPC's rejection that happened earlier this cycle.
                actor_in_call = (inp.get("attacker") or inp.get("caster") or "").strip().lower()
                if actor_in_call != name_lower:
                    continue
                candidates = result.get("candidates", [])
                if candidates:
                    if len(candidates) == 2:
                        targets = f"{candidates[0]} or {candidates[1]}"
                    else:
                        targets = ", ".join(candidates[:-1]) + f", or {candidates[-1]}"
                    return f"{name}, name your target — {targets}?"
        if any(c["name"] == "start_combat" and c["result"].get("ok") for c in self.tool_trace):
            return f"{name}, you're up — what do you do?"
        return f"{name}, what do you do?"

    def take_turn(self, player_input: str) -> str:
        """Resolve the player's action, then auto-run any following NPC turns.

        Context is rebuilt fresh each turn from a bounded narration window plus a
        live state snapshot — old tool_use/tool_result blocks are not carried forward
        because their effects are already encoded in self.state. Within this turn's
        _execute loops the tool results accumulate as normal so the agent can finish
        the multi-hop resolution coherently.

        Narration is folded into as few calls as possible:
          - Out of combat: the player-action _execute captures its terminating turn
            as the narration (no separate call).
          - In combat: the player action is resolved tool-only, then ONE unified
            _narrate_turn covers the player action plus every NPC beat in order.
          - Combat-over / end-of-run: dedicated _narrate_combat_over / _narrate_epilogue.
        Returns narration joined with the engine-sourced closing prompt.
        """
        self.tool_trace = []
        self.api_stats = []
        self.state.turn += 1
        self.state.combat_starting = False  # clear any stale barrier from a prior turn

        # Reset to a fresh bounded context; _execute will append this turn's messages.
        self.messages = self._build_turn_context()

        # --- Player's action ---
        # In combat, NPC beats are resolved after this _execute and folded into one
        # unified narration, so resolve the action tool-only here. Out of combat there
        # are no following beats, so the tool loop's terminating turn IS the narration
        # (capture_narration=True) — no separate narration call is spent.
        in_combat = bool(self.state.combat_order) and self.state.combat_round > 0
        if in_combat:
            finish = (
                "[Tool-use phase] Call the appropriate tools to resolve this action. "
                "Write no prose — the turn's narration is requested separately."
            )
        else:
            finish = (
                "[Tool-use phase] Call the appropriate tools to resolve this action. "
                "Once every tool has resolved, write your FINAL message as 1–3 sentences "
                "of in-world narration of what just happened (hit or miss, damage, spell "
                "effect, movement). Emit prose ONLY in that final message — never alongside "
                "a tool call. No tool JSON, no state dumps, no prompts, no meta-commentary."
            )
        player_prompt = (
            f"[Current state]\n{self._state_snapshot()}\n\n"
            f"Player: {player_input}\n\n"
            f"{finish}"
        )
        trace_len = len(self.tool_trace)
        player_narration = self._execute(player_prompt, capture_narration=not in_combat,
                                          stop_when_action_used=in_combat)
        self._maybe_end_combat()
        self.state.combat_starting = False  # clear barrier before NPC loop so NPCs can act

        # If combat ended during the player's own action, settle the verdict now.
        # _maybe_end_combat already did so when the *engine* ended it; this also covers
        # a *model*-issued end_combat (surrender/flee), which zeroes combat_round and
        # would otherwise slip past the verdict and leave the epilogue unfired.
        if in_combat and self.state.combat_round == 0:
            self._adjudicate_combat_outcome()

        # Check point 1: game_over from player phase (victory via move_scene, or party wipe).
        if self.state.game_over:
            # The action narration (captured, not streamed) comes first; emit it, then
            # the epilogue streams after it in order.
            self._emit(player_narration)
            if player_narration:
                self._emit("\n\n")
            epilogue = self._narrate_epilogue(self.state.game_outcome)
            combined = _sanitize_narration("\n\n".join(n for n in [player_narration, epilogue] if n))
            self.narration_history.append((player_input, combined))
            if len(self.narration_history) > NARRATION_WINDOW:
                self.narration_history = self.narration_history[-NARRATION_WINDOW:]
            _record_turn(self.state, player_input, combined)
            self.state.narrative.append({"turn": self.state.turn, "text": combined})
            self.full_trace.append({"turn": self.state.turn, "input": player_input, "calls": list(self.tool_trace), "api_calls": list(self.api_stats)})
            return combined

        # Did combat end during the player's own action (e.g. the killing blow)?
        combat_over_in_player_phase = any(
            c["name"] == "end_combat" for c in self.tool_trace[trace_len:]
        )

        # --- NPC turns and automatic death saves (only while combat is active) ---
        # Accumulate ordered combat beats (NPC actions and PC death saves); they are
        # folded into the single unified turn narration after the loop, alongside the
        # player's action — one narration call covers the whole exchange.
        combat_beats: list[dict] = []
        combat_ended_in_npc_phase = False

        if self.state.combat_order and self.state.combat_round > 0:
            current_key = self.state.combat_order[self.state.combat_index]
            # advance_first: only true if the active player HAS used their action.
            # action_used=False means start_combat just ran (player hasn't acted yet)
            # or the declared action was turn-guard rejected — in either case the
            # pointer stays where the engine left it; do NOT call next_turn.
            advance_first = self.state.action_used and current_key in self.state.party

            # Prompts for NPC turns the engine can't resolve; flushed as one _execute call.
            fallback_queue: list[str] = []

            def _flush_fallbacks() -> bool:
                """Batch-execute queued fallback prompts. Returns True if combat ended."""
                nonlocal combat_ended_in_npc_phase
                if not fallback_queue or self.state.game_over or self.state.combat_round == 0:
                    fallback_queue.clear()
                    return False
                self._execute("\n\n".join(fallback_queue))
                fallback_queue.clear()
                self._maybe_end_combat()
                if self.state.game_over or self.state.combat_round == 0:
                    combat_ended_in_npc_phase = True
                    return True
                return False

            for i in range(len(self.state.combat_order)):
                if advance_first or i > 0:
                    adv = tools.dispatch("next_turn", {}, self.state)
                    self.tool_trace.append({"name": "next_turn", "input": {}, "result": adv})
                    if not adv["ok"]:
                        break
                    active_key = adv["active"]
                    active_name = adv["active_name"]
                    active_round = adv["round"]
                else:
                    all_actors = {**self.state.party, **self.state.npcs}
                    active_key = current_key
                    active_name = all_actors[active_key].name if active_key in all_actors else active_key
                    active_round = self.state.combat_round

                if active_key in self.state.party:
                    # Flush any queued fallback NPC turns before stopping at this PC slot
                    # so those NPCs act before the PC's turn prompt is surfaced.
                    if _flush_fallbacks():
                        break
                    pc = self.state.party[active_key]
                    decision = tools._pc_turn_decision(pc)
                    if decision == "roll":
                        res = tools.dispatch("roll_death_save", {"character": pc.name}, self.state)
                        self.tool_trace.append({"name": "roll_death_save", "input": {"character": pc.name}, "result": res})
                        outcome = (
                            f"roll {res['roll']} → {res['result_kind']} "
                            f"(successes {res.get('successes', 0)}, failures {res.get('failures', 0)})"
                        )
                        combat_beats.append({"kind": "death_save", "name": active_name, "round": active_round, "outcome": outcome})
                        self._maybe_end_combat()
                        if self.state.game_over:
                            combat_ended_in_npc_phase = True
                            break
                        if self.state.combat_round == 0:
                            combat_ended_in_npc_phase = True
                            break
                        if res.get("result_kind") == "revived":
                            break  # PC is back up; closing prompt addresses them
                        continue  # save was this PC's whole turn; advance to next
                    elif decision == "break":
                        break  # conscious PC — stop and prompt
                    else:  # "skip" — stable/dead; next_turn should have caught this
                        continue

                # --- NPC turn ---
                npc = self.state.npcs.get(active_key)
                # Round-1 surprise: skip NPCs that weren't reached via next_turn
                # (i.e. the leading actor inspected directly after start_combat).
                if npc and npc.surprised and self.state.combat_round == 1:
                    self.state.record(f"surprised, skipping {active_key} ({npc.name})")
                    npc.surprised = False
                    continue
                resolution = tools.resolve_npc_action(npc, self.state) if npc else None

                if resolution is not None:
                    # Engine-resolved: no API call.
                    npc_args, npc_result = resolution
                    self.tool_trace.append({"name": "attack", "input": npc_args, "result": npc_result})
                    # Inject a brief context message so _narrate_turn has
                    # the same hit/miss/damage facts it would see from a tool_result.
                    _hit = npc_result.get("hit", False)
                    _detail = (
                        f"hit, {npc_result.get('damage', 0)} dmg → {npc_args['defender']} HP {npc_result.get('target_hp')}"
                        if _hit else "miss"
                    )
                    self.messages.append({"role": "user", "content": f"[Engine: {active_name} attacked {npc_args['defender']}: {_detail}]"})
                    combat_beats.append({"kind": "npc_action", "name": active_name, "round": active_round})
                    self._maybe_end_combat()
                    if self.state.game_over:
                        combat_ended_in_npc_phase = True
                        break
                    if self.state.combat_round == 0:
                        combat_ended_in_npc_phase = True
                        break
                else:
                    # Fallback: queue prompt; will be batched into one _execute call.
                    npc_exec_prompt = (
                        f"[Combat — Round {active_round}, {active_name}'s turn — Tool-use phase] "
                        f"Decide {active_name}'s action and execute it with the appropriate tool(s). "
                        f"Write no prose — narration is requested separately."
                    )
                    fallback_queue.append(npc_exec_prompt)
                    combat_beats.append({"kind": "npc_action", "name": active_name, "round": active_round})

            # Flush any remaining fallbacks after the loop completes.
            _flush_fallbacks()

        # Check point 2: game_over from NPC phase (defeat). Narrate the exchange that
        # led here (player action + NPC beats, folded into one call), then the epilogue.
        if self.state.game_over:
            if combat_beats:
                exchange = self._narrate_turn(player_input, combat_beats)  # streams live
            else:
                exchange = player_narration  # captured, not streamed
                self._emit(exchange)
            if exchange:
                self._emit("\n\n")
            epilogue = self._narrate_epilogue(self.state.game_outcome)
            combined = _sanitize_narration("\n\n".join(n for n in [exchange, epilogue] if n))
            self.narration_history.append((player_input, combined))
            if len(self.narration_history) > NARRATION_WINDOW:
                self.narration_history = self.narration_history[-NARRATION_WINDOW:]
            _record_turn(self.state, player_input, combined)
            self.state.narrative.append({"turn": self.state.turn, "text": combined})
            self.full_trace.append({"turn": self.state.turn, "input": player_input, "calls": list(self.tool_trace), "api_calls": list(self.api_stats)})
            return combined

        # One narration call for the whole turn:
        #  - combat ended this turn → dedicated two-paragraph close (covers the finishing blow);
        #  - in-combat turn → unified player-action + NPC-beats narration via _narrate_turn.
        #    This fires whenever we were in combat at the start of the turn, even when NO
        #    NPC beats accumulated (combat_beats empty) — e.g. a PC acts and the next
        #    combatant in initiative is another PC. In combat the player action is
        #    resolved tool-only (player_narration == ""), so its prose exists ONLY as
        #    beat 1 of _narrate_turn; gating on combat_beats here would silently drop it.
        #  - out-of-combat turn → the terminating turn already captured by _execute.
        # The first two stream live inside their narration call; the last is captured
        # (not streamed) and is emitted here as a chunk so the sink sees it in order.
        if combat_over_in_player_phase or combat_ended_in_npc_phase:
            narration = self._narrate_combat_over()
        elif in_combat or combat_beats:
            narration = self._narrate_turn(player_input, combat_beats)
        else:
            narration = player_narration
            self._emit(_sanitize_narration(narration))

        # Persist narration (not the closing prompt) to the rolling window.
        combined = _sanitize_narration(narration)
        self.narration_history.append((player_input, combined))
        if len(self.narration_history) > NARRATION_WINDOW:
            self.narration_history = self.narration_history[-NARRATION_WINDOW:]
        _record_turn(self.state, player_input, combined)
        self.state.narrative.append({"turn": self.state.turn, "text": combined})

        self.full_trace.append({"turn": self.state.turn, "input": player_input, "calls": list(self.tool_trace), "api_calls": list(self.api_stats)})

        # Engine-sourced trailers, kept separate so they're not stored in history:
        # a one-time initiative readout when combat just started, then the active-actor
        # prompt. Both are deterministic so the order is always shown to the player.
        banner = self._initiative_banner()
        closing = self._closing_prompt()
        trailer = "\n\n".join(p for p in [banner, closing] if p)
        if trailer:
            self._emit(("\n\n" if combined else "") + trailer)
        return "\n\n".join(p for p in [combined, banner, closing] if p)


def _json(obj) -> str:
    return json.dumps(obj)
