#!/usr/bin/env python3
"""Simple CLI to manage the skill matrix.

Usage examples:
  python3 scripts/manage_skill_matrix.py list
  python3 scripts/manage_skill_matrix.py add-skill "python" "Backend python"
  python3 scripts/manage_skill_matrix.py set-member "alice@example.com" "{\"python\": \"senior\"}"
"""
import sys
import json
from utilities.skill_matrix import define_skill, set_member_skills, load, save, list_skills, list_members, get_member_skills


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    cmd = argv[1]
    if cmd == "list":
        data = load()
        print(json.dumps(data, indent=2))
        return 0
    if cmd == "add-skill" and len(argv) >= 3:
        name = argv[2]
        desc = argv[3] if len(argv) >= 4 else ""
        define_skill(name, desc)
        print("skill added")
        return 0
    if cmd == "set-member" and len(argv) >= 4:
        member = argv[2]
        skills = json.loads(argv[3])
        set_member_skills(member, skills)
        print("member skills set")
        return 0
    if cmd == "get-member" and len(argv) >= 3:
        member = argv[2]
        print(json.dumps(get_member_skills(member), indent=2))
        return 0
    print("unknown command")
    return 2


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
