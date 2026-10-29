# Excel / Numbers / spreadsheets

For files, use tools instead of the mouse: read_document to read an .xlsx or
.csv, convert_file for csv<->xlsx, make_chart to draw a chart, and python_eval
with pandas/openpyxl to analyse or write a workbook.

A workbook you create should look finished, in one python_eval: bold header
row with a fill colour, frozen header (ws.freeze_panes = "A2"), column widths
fitted to the content, number formats (currency, %, dates), formulas such as
=SUM() for totals instead of typed results, an Excel table or autofilter on
the data, and an openpyxl chart when the data has a trend or comparison. Then
read_document the file to check it before reporting.

## When coaching in the app
- Formula basics: start with `=`; SUM, AVERAGE, COUNTIF(S), SUMIF(S), IF,
  VLOOKUP / XLOOKUP, INDEX+MATCH, TEXT, TODAY. Absolute reference `$A$1`.
- Pivot table: select the data > Insert > PivotTable > drag fields to Rows /
  Columns / Values.
- Chart: select the range > Insert > Recommended Charts or a chart type.
- Sort/filter: Data > Sort / Filter. Freeze header: View > Freeze Panes.
- Conditional formatting: Home > Conditional Formatting.
- Numbers (Apple): same ideas; formulas via the `=` key; charts via the Chart
  toolbar button.
