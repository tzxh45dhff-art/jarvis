# Word / Pages / documents

A NEW Word document is one make_document call with a .docx path: Markdown in,
a designed Word file out (real headings, lists, tables, pictures, charts,
diagrams, page numbers). Short documents - one page, letters, notes, plans -
take cover=false and toc=false. Do not hand-write python-docx for a new file.
CHANGING an existing .docx: python-docx via python_eval (keep its styles), then
read_document to check. Other tools: convert_file (docx/rtf/txt/html),
read_document to read.

## When coaching in the app
- Styles (Heading 1/2, Normal) are on the Home tab (Word) or the Format
  sidebar (Pages) - use them instead of manual font sizes.
- Table of contents: References > Table of Contents (Word).
- Track changes: Review > Track Changes. Comments: Review > New Comment.
- Page numbers: Insert > Page Number. Margins: Layout > Margins.
- Export PDF: File > Save As > PDF (Word) / File > Export To > PDF (Pages).
