# The canonical way to consume scryfall_stamp.py's output.
#
# This lives in the PUBLIC repository, beside the producer, because that is
# where the format is decided — and because a test that reads a sibling
# private repository silently skips wherever that sibling is absent, which is
# every clone of this repo and its CI.
#
# The private build workflow must contain these lines VERBATIM; a test asserts
# both that they work and that the workflow still matches. Token checks were
# tried and are not a contract: looking for two `read` substrings let the
# command inside the process substitution change while both reads survived.
#
# $STAMPS is the path to the producer, so the same lines serve a checkout at
# any prefix. Positional, never eval: these values come from a third-party API.
{ read -r oracle_cards; read -r rulings; } < <(python3 "$STAMPS")
[ -n "$oracle_cards" ] && [ -n "$rulings" ] || { echo "no stamps"; exit 1; }
