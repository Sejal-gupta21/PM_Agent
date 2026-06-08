"""Streamlit page to review and edit the skill matrix.

Run with:
  streamlit run app/skill_matrix_ui.py

The page loads `data/skill_matrix.json`, lets you pick a member,
assign skills and proficiency levels, add/remove skills, and save.
"""
from __future__ import annotations

import streamlit as st
from typing import Dict
from utilities.skill_matrix import load, save, list_skills, list_members, get_member_skills, set_member_skills, define_skill

LEVELS = ["novice", "junior", "intermediate", "senior", "expert"]


@st.cache_data
def load_matrix():
    return load()


def persist_matrix(data):
    save(data)


def main():
    st.set_page_config(page_title="Skill Matrix", layout="wide")
    st.title("Skill Matrix — Review & Edit")

    data = load_matrix()
    skills = sorted(list_skills().keys())
    members = sorted(list_members().keys())

    col1, col2 = st.columns([1, 3])

    with col1:
        st.header("Members")
        sel_member = st.selectbox("Select member", ["<new member>"] + members)
        if sel_member == "<new member>":
            new_m = st.text_input("New member identifier (email or username)")
            if new_m:
                sel_member = new_m
                if sel_member not in members:
                    members.append(sel_member)
        if st.button("Refresh"):
            st.cache_data.clear()
            st.experimental_rerun()

        st.markdown("---")
        st.header("Global Skills")
        new_skill = st.text_input("Add new skill name")
        new_skill_desc = st.text_input("Description (optional)")
        if st.button("Add skill") and new_skill:
            try:
                define_skill(new_skill, new_skill_desc)
                st.success(f"Added skill {new_skill}")
                st.cache_data.clear()
                st.experimental_rerun()
            except Exception as e:
                st.error(f"Failed to add skill: {e}")

    with col2:
        st.header("Member Skills")
        if not sel_member:
            st.info("Select a member on the left to edit skills.")
            return
        st.subheader(f"Editing: {sel_member}")
        member_skills = get_member_skills(sel_member)
        # show current skills with selectboxes for levels
        if not member_skills:
            st.info("No skills assigned yet for this member.")
        updated = dict(member_skills) if member_skills else {}

        if updated:
            st.write("Assigned skills")
            for skill, lvl in list(updated.items()):
                cols = st.columns([3, 2, 1])
                cols[0].write(skill)
                new_lvl = cols[1].selectbox(f"lvl_{skill}", LEVELS, index=LEVELS.index(lvl) if lvl in LEVELS else 0, key=f"lvl_{sel_member}_{skill}")
                if new_lvl != lvl:
                    updated[skill] = new_lvl
                if cols[2].button(f"Remove_{skill}", key=f"rm_{sel_member}_{skill}"):
                    updated.pop(skill, None)
                    st.experimental_rerun()

        st.markdown("---")
        st.write("Assign additional skills")
        add_skill = st.selectbox("Skill to add", ["<none>"] + skills, index=0)
        add_level = st.selectbox("Proficiency", LEVELS, index=1)
        if add_skill and add_skill != "<none>":
            if st.button("Add skill to member"):
                updated[add_skill] = add_level
                st.success(f"Assigned {add_skill} -> {add_level} to {sel_member}")
                # write through helper
                set_member_skills(sel_member, updated)
                st.experimental_rerun()

        st.markdown("---")
        if st.button("Save changes"):
            try:
                set_member_skills(sel_member, updated)
                persist_matrix(load())
                st.success("Saved skill matrix")
            except Exception as e:
                st.error(f"Failed to save: {e}")

        if st.button("Export to stdout (debug)"):
            st.code(load())


if __name__ == "__main__":
    main()
