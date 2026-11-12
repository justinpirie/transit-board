# Home Assistant side

The Transit Board app publishes `sensor.commute_leave_in` itself and reads your
person, calendars and the `Commute today` toggle through HA's API, so **nothing
here goes in `configuration.yaml`**. These files are the UI-created pieces,
exported from a working install.

## Setup checklist

| # | What | Where |
|---|------|-------|
| 1 | **Toggle helper** `input_boolean.commute_today`, named "Commute today". After creating it, turn it **on**: a new toggle starts off, and off vetoes the commute. | Settings → Devices & services → Helpers |
| 2 | **Zone** named `Office` at your workplace (radius ~150 m). Your person state becomes `Office` there, which the app maps to `to_home`. | Settings → Areas & zones |
| 3 | **App options**: `person_entity`, `work_calendars`, `holiday_calendar`, plus override `location_directions={home: to_office, office: to_home}` | Transit Board app → Configuration |
| 4 | **Automations** in `automations/`. Swap `person.you` and `notify.mobile_app_your_phone` for yours. | Settings → Automations → Create → ⋮ → Edit in YAML |
| 5 | **Dashboard**: `dashboard_commute.yaml` | New dashboard → ⋮ → Raw configuration editor |

## Automations

| File | Fires | Does |
|------|-------|------|
| `leave_now_morning.yaml` | sensor drops below 3 min, weekdays 06:30–11:00, at home | "Leave now" push with the recommendation and backup |
| `leave_now_evening.yaml` | same, 15:00–20:30, in `zone.office` | "Head home" push |
| `reset_commute_today.yaml` | 04:30 daily | Turns `Commute today` back on, so a "day off" lasts one day |
| `ask_on_public_holidays.yaml` | 06:15 weekdays, if today is a public holiday | Actionable push: commuting today? (no answer = yes) |
| `handle_holiday_answer.yaml` | a button on that push | Sets the toggle and confirms |

Pushes are iOS `time-sensitive`, and share a `tag` so they replace each other
rather than stacking.

## Gotchas these configs already handle

- **Restart re-arm:** `numeric_state` fires when a sensor goes `unavailable` → number.
  After an HA restart the app re-creates the sensor, so a guard condition only
  accepts a real numeric drop.
- **`variables:` ordering:** HA stores a `variables` block with its keys sorted, so a
  key that reads another key in the same block can render before it's defined.
  `icon` (reads `r`) therefore sits in its own step.
- **Cooldown:** after you've been told to leave, the next bus dropping below 3 min
  shouldn't ping again, so pushes are spaced at least 15 min apart.
