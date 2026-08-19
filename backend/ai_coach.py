"""Update 1.9.4: KI-Auswertung + Chat -- talks to the Anthropic API.

Two features, both requiring ANTHROPIC_API_KEY to be set as an environment
variable (Railway: Project -> Variables). Neither one is wired into any
existing endpoint's happy path without the key -- both raise a clear
RuntimeError that main.py turns into a 503 with an explanatory message, so
the rest of the app keeps working fine if the key isn't configured yet.

1. analyze_checkin() -- runs right after a workout is finished. Takes the
   just-logged sets plus a small "wie schwer/wie geschlaucht" questionnaire
   and asks the model for: next-session weight/rep suggestions per
   exercise, a couple of concrete tips, and any muscle groups that have
   been getting neglected lately.
2. chat_reply() -- a free-form fitness Q&A chat ("wie kann ich meine Adern
   mehr sehen lassen"), with a little conversation history for context.
"""
from __future__ import annotations

import json
import os
from typing import Optional

# Configurable via env var so this can be bumped without a code change if
# Anthropic ships a newer default -- this is the current (as of writing)
# Claude Sonnet model id.
_DEFAULT_MODEL = "claude-sonnet-4-5-20250929"


def _model() -> str:
    return os.environ.get("ANTHROPIC_MODEL") or _DEFAULT_MODEL


def _client():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    import anthropic  # imported lazily so the app still boots without the package/key
    return anthropic.Anthropic(api_key=api_key)


def is_configured() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _extract_text(message) -> str:
    parts = []
    for block in message.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "".join(parts).strip()


CHECKIN_SYSTEM = """Du bist der eingebaute Trainings-Coach der Fitness-App "bymgro" \
(Push/Pull-Split, Fokus Kraft- und Muskelaufbau). Nach jedem abgeschlossenen Training \
bekommst du: die geloggten Sätze (Übung, Gewicht, Wiederholungen), eine Schwierigkeits- \
und eine Erschöpfungs-Einschätzung des Nutzers (1=sehr leicht/frisch, 5=extrem schwer/platt), \
eine optionale Notiz, und einen kurzen Überblick, wie oft welche Muskelgruppe zuletzt \
trainiert wurde.

Antworte AUSSCHLIESSLICH mit einem validen JSON-Objekt (keine Erklärung drumherum, kein \
Markdown-Codeblock, kein Text vor oder nach dem JSON) mit exakt diesen Feldern:
{
  "suggestions": [{"exercise": "<Übungsname exakt wie gegeben>", "next_weight": <Zahl oder null>, \
"next_reps": <Zahl oder null>, "note": "<max. 12 Wörter Begründung>"}],
  "tips": ["<kurzer, konkreter Tipp>"],
  "neglected_muscles": ["<Muskelgruppe>"]
}

Regeln: "suggestions" nur für Übungen, die tatsächlich in den geloggten Sätzen vorkommen -- \
sinnvolle kleine Progression (z.B. +1 bis +2.5kg oder +1 Wdh) wenn die Schwierigkeit niedrig \
war, gleich bleiben oder leicht reduzieren wenn sehr schwer/stark erschöpft. "tips" maximal 3 \
Einträge, kurz und konkret (Technik, Regeneration, Ernährung) -- keine generischen \
Plattitüden wie "trink genug Wasser". "neglected_muscles" nur befüllen wenn im Überblick \
eine Muskelgruppe klar selten vorkommt, sonst leeres Array. Immer auf Deutsch, direkt und \
knapp wie ein guter Personal Trainer, kein Disclaimer-Gerede."""


def analyze_checkin(exercises_summary: str, difficulty: int, exhaustion: int,
                     note: Optional[str], recent_muscle_summary: str) -> dict:
    client = _client()
    if client is None:
        raise RuntimeError("ANTHROPIC_API_KEY not configured")
    user_content = (
        f"Geloggte Sätze dieses Trainings:\n{exercises_summary}\n\n"
        f"Schwierigkeit (1=sehr leicht, 5=extrem schwer): {difficulty}\n"
        f"Erschöpfung (1=frisch, 5=komplett platt): {exhaustion}\n"
        f"Notiz vom Nutzer: {note or '(keine)'}\n\n"
        f"Muskelgruppen-Häufigkeit der letzten Wochen:\n{recent_muscle_summary}"
    )
    msg = client.messages.create(
        model=_model(),
        max_tokens=1024,
        system=CHECKIN_SYSTEM,
        messages=[{"role": "user", "content": user_content}],
    )
    text = _extract_text(msg)
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        # Model didn't return clean JSON -- surface *something* useful
        # rather than throwing the whole check-in away.
        return {"suggestions": [], "tips": [text[:400]] if text else [], "neglected_muscles": []}
    data.setdefault("suggestions", [])
    data.setdefault("tips", [])
    data.setdefault("neglected_muscles", [])
    return data


CHAT_SYSTEM = """Du bist der eingebaute Fitness-Coach-Chat der App "bymgro". Der Nutzer \
trainiert im Push/Pull-Split, Fokus Muskelaufbau/Kraft/Optik. Beantworte Fragen zu Training, \
Ernährung, Regeneration, Supplements, Aussehen (z.B. Definition/Vaskularität) etc. direkt, \
konkret und knapp -- wenige Sätze, keine Roman-Antworten, Aufzählungen nur wenn wirklich \
nötig. Kein Disclaimer-Gerede, aber bei medizinischen Themen (Schmerzen, Verletzungen, \
Medikamente, Essstörungen) ehrlich sagen dass das kein Ersatz für einen Arzt ist und dort \
nicht einfach einen Trainingsplan draufsatteln. Duze den Nutzer, sprich Deutsch."""


def chat_reply(history: list[dict], user_message: str, profile_summary: str) -> str:
    client = _client()
    if client is None:
        raise RuntimeError("ANTHROPIC_API_KEY not configured")
    messages = [{"role": m["role"], "content": m["content"]} for m in history[-10:]]
    messages.append({"role": "user", "content": user_message})
    system = CHAT_SYSTEM + f"\n\nNutzer-Kontext: {profile_summary}"
    msg = client.messages.create(
        model=_model(),
        max_tokens=700,
        system=system,
        messages=messages,
    )
    return _extract_text(msg)
