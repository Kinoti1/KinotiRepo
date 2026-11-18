# Metasync test reports

These files contain synthetic test records only; the titles, people, identifiers, and institutions are fictional.

- `metadata_cleanup_duplicates_part1.csv` includes whitespace, inconsistent language labels, a missing creator and ISBN, an uncertain year, and an exact repeated row.
- `metadata_cleanup_duplicates_part2.csv` includes related records also present in part 1, with minor spelling and creator-format differences for duplicate review.

To try the workflow, sign in to Metasync, upload each CSV separately from **Upload Records**, then use **Harmonization** to clean each report and **Duplicate Detection** to review matching records across the workspace. Do not upload both copies of the same file; they are different test reports intended to share some records.