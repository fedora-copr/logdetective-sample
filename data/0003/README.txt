Source: https://kojipkgs.fedoraproject.org//work/tasks/9915/128879915/build.log

The error is hidden inside the file and not at the end. The error:
```
/builddir/build/BUILD/LabPlot-2.11.80_20241117.082905.4e770ae-build/labplot-4e770ae2d988362dca637aef2b74610a4a1456c2/src/backend/datasources/filters/XLSXFilter.cpp: In member function ‘QXlsx::Cell::CellType XLSXFilterPrivate::columnTypeInRange(int, const QXlsx::CellRange&) const’:
/builddir/build/BUILD/LabPlot-2.11.80_20241117.082905.4e770ae-build/labplot-4e770ae2d988362dca637aef2b74610a4a1456c2/src/backend/datasources/filters/XLSXFilter.cpp:795:62: error: unable to deduce ‘const auto*’ from ‘QXlsx::Document::cellAt(int, int) const(row, ((int)column))’
  795 |                         const auto* cell = m_document->cellAt(row, column);
      |                                            ~~~~~~~~~~~~~~~~~~^~~~~~~~~~~~~
/builddir/build/BUILD/LabPlot-2.11.80_20241117.082905.4e770ae-build/labplot-4e770ae2d988362dca637aef2b74610a4a1456c2/src/backend/datasources/filters/XLSXFilter.cpp:795:74: note:   mismatched types ‘const auto*’ and ‘std::shared_ptr<QXlsx::Cell>’
  795 |                         const auto* cell = m_document->cellAt(row, column);
      |                                                                          ^
```
