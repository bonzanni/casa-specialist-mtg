# The canonical way to consume scryfall_stamp.py's output.
#
# This lives in the PUBLIC repository, beside the producer, because that is
# where the format is decided. The private build workflow must contain this
# snippet verbatim; a test asserts both that it works and that the workflow
# still matches it.
#
# Keeping it here rather than only in the workflow is deliberate: a test that
# reads a sibling repository silently skips wherever that sibling is absent,
# which is every clone of the public repo and its CI. A change to the
# formatter would then pass everything and wedge the scheduled build.
#
# Positional, never eval: these values come from a third-party API.
{ read -r oracle_cards; read -r rulings; } < <(python3 scripts/scryfall_stamp.py)
[ -n "$oracle_cards" ] && [ -n "$rulings" ] || {
  echo "scryfall_stamp produced nothing"; exit 1; }
