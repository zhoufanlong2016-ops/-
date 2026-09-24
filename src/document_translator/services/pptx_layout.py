"""PowerPoint-native, conservative layout fitting for translated PPTX files."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
import subprocess
import textwrap


@dataclass(frozen=True)
class PptxLayoutOutcome:
    report_path: Path
    reviewed: int
    unresolved: int


class PptxLayoutService:
    """Use desktop PowerPoint bounds instead of unsafe character-count fitting."""

    def __init__(self, *, minimum_font_size: float = 8.0) -> None:
        if minimum_font_size <= 0:
            raise ValueError("minimum_font_size must be positive")
        self.minimum_font_size = minimum_font_size

    def fit_file(self, source: str | Path, destination: str | Path, report_path: str | Path) -> PptxLayoutOutcome:
        source, destination, report_path = Path(source).resolve(), Path(destination).resolve(), Path(report_path).resolve()
        if os.name != "nt":
            raise RuntimeError("PPTX layout fitting requires desktop PowerPoint on Windows")
        if not source.is_file() or not destination.is_file():
            raise FileNotFoundError("source and translated PPTX must both exist")
        report_path.parent.mkdir(parents=True, exist_ok=True)
        script_path = report_path.with_suffix(".layout.ps1")
        script_path.write_text(self._script(), encoding="utf-8-sig", newline="\r\n")
        try:
            completed = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path),
                 "-Source", str(source), "-Target", str(destination), "-Report", str(report_path),
                 "-MinimumFont", str(self.minimum_font_size)],
                check=False, capture_output=True, text=True, timeout=180,
            )
        finally:
            script_path.unlink(missing_ok=True)
        if completed.returncode:
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(f"PowerPoint layout fitting failed: {detail}")
        report = json.loads(report_path.read_text(encoding="utf-8-sig"))
        return PptxLayoutOutcome(report_path=report_path, reviewed=int(report["reviewed"]), unresolved=int(report["unresolved"]))

    @staticmethod
    def _script() -> str:
        return textwrap.dedent(r'''
            param([string]$Source, [string]$Target, [string]$Report, [double]$MinimumFont)
            $ErrorActionPreference = 'Stop'
            $msoAutoSizeNone = 0; $msoAutoSizeTextToFitShape = 2
            $msoGroup = 6; $msoTrue = -1
            $records = New-Object System.Collections.Generic.List[object]
            function Get-TextFrames($Shape, [string]$Path, [bool]$InTable) {
              $items = @()
              if ($Shape.Type -eq $msoGroup) {
                for ($i=1; $i -le $Shape.GroupItems.Count; $i++) { $items += Get-TextFrames $Shape.GroupItems.Item($i) "$Path/g$i" $InTable }
                return $items
              }
              if ($Shape.HasTable -eq $msoTrue) {
                for ($r=1; $r -le $Shape.Table.Rows.Count; $r++) { for ($c=1; $c -le $Shape.Table.Columns.Count; $c++) {
                  $items += [pscustomobject]@{ Shape=$Shape.Table.Cell($r,$c).Shape; Container=$Shape; Path="$Path/t$r,$c"; InTable=$true }
                }}
                return $items
              }
              if ($Shape.HasTextFrame -eq $msoTrue -and $Shape.TextFrame2.HasText -eq $msoTrue) {
                $items += [pscustomobject]@{ Shape=$Shape; Container=$Shape; Path=$Path; InTable=$InTable }
              }
              return $items
            }
            function Is-Overflow($Shape, $Slide, [bool]$CheckPageBounds = $true) {
              # Use TextFrame2 consistently: translation writes TextRange2
              # font properties and PowerPoint can report different bounds for
              # the legacy TextFrame and TextFrame2 APIs. Mixing them causes
              # false overflow reports and needless reduction to 8 pt.
              $tf = $Shape.TextFrame2; $tr = $tf.TextRange
              $availableHeight = $Shape.Height - $tf.MarginTop - $tf.MarginBottom
              $slideWidth = $Slide.Parent.PageSetup.SlideWidth
              $slideHeight = $Slide.Parent.PageSetup.SlideHeight
              # TextRange2 bounds are already slide-relative coordinates.
              # Adding Shape.Left/Top again produces false page overflow.
              $left = $tr.BoundLeft
              $top = $tr.BoundTop
              $right = $left + $tr.BoundWidth
              $bottom = $top + $tr.BoundHeight
              if (-not $CheckPageBounds) { return ($tr.BoundHeight -gt ($availableHeight + 2.0)) }
              return (($tr.BoundHeight -gt ($availableHeight + 2.0)) -or
                      ($left -lt -0.5) -or ($right -gt ($slideWidth + 0.5)) -or
                      ($top -lt -0.5) -or ($bottom -gt ($slideHeight + 0.5)))
            }
            function Get-MasterSafeBottom($Slide) {
              $slideHeight = $Slide.Parent.PageSetup.SlideHeight
              $slideWidth = $Slide.Parent.PageSetup.SlideWidth
              $safeBottom = $slideHeight
              $foundBoundary = $false
              # Footer rules are often stored on the master/custom layout and
              # therefore do not appear in Slide.Shapes.  Treat a thin line or
              # near-full-width rectangle in the bottom 15% as a hard boundary.
              foreach ($container in @($Slide.Master, $Slide.CustomLayout)) {
                if ($null -eq $container) { continue }
                for ($i=1; $i -le $container.Shapes.Count; $i++) {
                  $candidate = $container.Shapes.Item($i)
                  try {
                    $nearBottom = ($candidate.Top -gt ($slideHeight * 0.80))
                    $wide = ($candidate.Width -gt ($slideWidth * 0.50))
                    $thin = ($candidate.Height -le 10)
                    # Footer graphics in this deck are pictures, not line
                    # objects.  Include near-full-width pictures as boundary
                    # elements, but do not reserve a hard-coded page band.
                    $lineLike = ($candidate.Type -eq 9 -or $thin -or $candidate.Type -eq 13)
                    if ($nearBottom -and $wide -and $lineLike) {
                      $safeBottom = [math]::Min($safeBottom, $candidate.Top - 6)
                      $foundBoundary = $true
                    }
                  } catch { }
                }
              }
              # If a master boundary cannot be identified, do not expand into
              # the page edge; fitting must remain inside the original frame.
              return [pscustomobject]@{ Bottom=$safeBottom; BoundaryFound=$foundBoundary }
            }
            function Fit-Measured($Shape, $Slide, [double]$MinimumFont) {
              # The desktop app measures after every font update.  This is a
              # bounded measurement loop, not a character-count heuristic.
              for ($attempt=0; $attempt -lt 6 -and (Is-Overflow $Shape $Slide); $attempt++) {
                $tf = $Shape.TextFrame2; $available = $Shape.Height - $tf.MarginTop - $tf.MarginBottom
                $rendered = $tf.TextRange.BoundHeight; $current = $tf.TextRange.Font.Size
                if ($current -le $MinimumFont -or $rendered -le 0) { break }
                $next = [math]::Max($MinimumFont, [math]::Floor(($current * $available / $rendered * 0.98) * 10) / 10)
                if ($next -ge $current) { $next = [math]::Max($MinimumFont, $current - 0.5) }
                $tf.AutoSize = $msoAutoSizeNone; $tf.TextRange.Font.Size = [single]$next
              }
            }
            function Expand-BodyHeightSafely($Shape, $Slide, [double]$SafeBottom) {
              # Geometry is immutable for decoration, title, grouped and table
              # objects.  A standalone body text box may grow only downward and
              # only by the measured height it lacks.
              $tf = $Shape.TextFrame; $needed = $tf.TextRange.BoundHeight - ($Shape.Height - $tf.MarginTop - $tf.MarginBottom)
              if ($needed -le 0.5) { return $false }
              $maxBottom = $SafeBottom
              foreach ($other in $Slide.Shapes) {
                if ($other.Id -eq $Shape.Id) { continue }
                $horizontalOverlap = (($Shape.Left -lt ($other.Left + $other.Width)) -and (($Shape.Left + $Shape.Width) -gt $other.Left))
                if ($horizontalOverlap -and $other.Top -gt $Shape.Top) { $maxBottom = [math]::Min($maxBottom, $other.Top) }
              }
              $newHeight = [math]::Min($Shape.Height + $needed, $maxBottom - $Shape.Top)
              if ($newHeight -le ($Shape.Height + 0.5)) { return $false }
              $Shape.Height = [single]$newHeight
              return $true
            }
            function Fit-TableMeasured($TableShape, $SourceTableShape, [double]$MinimumFont, $Slide, [double]$SafeBottom) {
              # Do not infer overflow from total table height: PowerPoint may
              # auto-grow a row by a fraction even when every cell still fits.
              # The old height-ratio rule therefore shrank valid 18 pt text to
              # the minimum.  Only the measured per-cell overflow ratio below
              # is allowed to trigger uniform table scaling.  Restore the
              # original table envelope first so translated rows cannot push
              # into the footer area merely because PowerPoint auto-grew them.
              $maxBottom = if ($SafeBottom -gt 0) { $SafeBottom } else { $Slide.Parent.PageSetup.SlideHeight }
              $naturalBottom = $TableShape.Top + $TableShape.Height
              if ($naturalBottom -gt ($maxBottom + 0.5)) {
                # Fit the whole table envelope with one uniform factor.  This
                # is the authoritative guard against a table covering a footer
                # line; individual cell checks alone cannot see that collision.
                $availableTable = [math]::Max(1.0, $maxBottom - $TableShape.Top)
                $scale = [math]::Min(1.0, ($availableTable / [math]::Max(1.0, $TableShape.Height)) * 0.98)
                for ($rr=1; $rr -le $TableShape.Table.Rows.Count; $rr++) { for ($cc=1; $cc -le $TableShape.Table.Columns.Count; $cc++) {
                  $cell = $TableShape.Table.Cell($rr,$cc).Shape
                  if ($cell.TextFrame2.HasText -eq $msoTrue) {
                    $current = $cell.TextFrame2.TextRange.Font.Size
                    if ($current -gt $MinimumFont) { $cell.TextFrame2.TextRange.Font.Size = [single][math]::Max($MinimumFont, [math]::Floor(($current * $scale) * 10) / 10) }
                  }
                }}
                $TableShape.Height = [single]$availableTable
              }
              # PowerPoint may auto-grow the table again after a height write.
              # Close the loop on the actual table envelope, applying one
              # uniform font factor per pass until the bottom is safe.
              for ($outer=0; $outer -lt 8; $outer++) {
                $actualBottom = $TableShape.Top + $TableShape.Height
                if ($actualBottom -le ($maxBottom + 0.5)) { break }
                $availableTable = [math]::Max(1.0, $maxBottom - $TableShape.Top)
                $factor = [math]::Max(0.55, ($availableTable / [math]::Max(1.0, $TableShape.Height)) * 0.96)
                $changed = $false
                for ($rr=1; $rr -le $TableShape.Table.Rows.Count; $rr++) { for ($cc=1; $cc -le $TableShape.Table.Columns.Count; $cc++) {
                  $cell = $TableShape.Table.Cell($rr,$cc).Shape
                  if ($cell.TextFrame2.HasText -eq $msoTrue) {
                    $current = $cell.TextFrame2.TextRange.Font.Size
                    if ($current -gt $MinimumFont) { $cell.TextFrame2.TextRange.Font.Size = [single][math]::Max($MinimumFont, [math]::Floor(($current * $factor) * 10) / 10); $changed = $true }
                  }
                }}
                if (-not $changed) { break }
              }
              for ($attempt=0; $attempt -lt 6; $attempt++) {
                $ratio = 1.0
                for ($r=1; $r -le $TableShape.Table.Rows.Count; $r++) { for ($c=1; $c -le $TableShape.Table.Columns.Count; $c++) {
                  $cell = $TableShape.Table.Cell($r,$c).Shape
                  if ($cell.TextFrame2.HasText -eq $msoTrue) {
                    $available = $cell.Height - $cell.TextFrame2.MarginTop - $cell.TextFrame2.MarginBottom
                    if ($available -gt 0) { $ratio = [math]::Max($ratio, $cell.TextFrame2.TextRange.BoundHeight / $available) }
                  }
                }}
                # Small differences are PowerPoint's line-box rounding and do
                # not justify changing the table's font size.
                if ($ratio -le 1.10) { return }
                $changed = $false
                for ($r=1; $r -le $TableShape.Table.Rows.Count; $r++) { for ($c=1; $c -le $TableShape.Table.Columns.Count; $c++) {
                  $cell = $TableShape.Table.Cell($r,$c).Shape; $current = $cell.TextFrame2.TextRange.Font.Size
                  if ($current -gt $MinimumFont) { $cell.TextFrame2.TextRange.Font.Size = [single][math]::Max($MinimumFont, [math]::Floor(($current / $ratio * 0.98) * 10) / 10); $changed = $true }
                }}
                if (-not $changed) { return }
              }
            }
            $app = $null; $sourcePresentation = $null; $targetPresentation = $null
            try {
              $app = New-Object -ComObject PowerPoint.Application
              $app.Visible = $msoTrue
              $sourcePresentation = $app.Presentations.Open($Source, $msoTrue, $msoFalse, $msoFalse)
              $targetPresentation = $app.Presentations.Open($Target, $msoFalse, $msoFalse, $msoFalse)
              # Shared-master fallback requested by the user: derive one
              # document-wide boundary from the lowest source text/table on
              # every page, then use the minimum of those bottoms everywhere.
              $globalSourceBottom = $targetPresentation.PageSetup.SlideHeight
              for ($srcIndex=1; $srcIndex -le $sourcePresentation.Slides.Count; $srcIndex++) {
                $srcSlide = $sourcePresentation.Slides.Item($srcIndex); $srcItems = @()
                for ($srcShapeIndex=1; $srcShapeIndex -le $srcSlide.Shapes.Count; $srcShapeIndex++) { $srcItems += Get-TextFrames $srcSlide.Shapes.Item($srcShapeIndex) "s$srcShapeIndex" $false }
                $pageBottom = 0.0
                foreach ($srcItem in $srcItems) { try { $pageBottom = [math]::Max($pageBottom, $srcItem.Shape.Top + $srcItem.Shape.Height) } catch { } }
                if ($pageBottom -gt 0) { $globalSourceBottom = [math]::Min($globalSourceBottom, $pageBottom) }
              }
              for ($slideIndex=1; $slideIndex -le $targetPresentation.Slides.Count; $slideIndex++) {
                $slide = $targetPresentation.Slides.Item($slideIndex); $sourceSlide = $sourcePresentation.Slides.Item($slideIndex)
                $outItems = @(); $sourceItems = @()
                for ($shapeIndex=1; $shapeIndex -le $slide.Shapes.Count; $shapeIndex++) { $outItems += Get-TextFrames $slide.Shapes.Item($shapeIndex) "s$shapeIndex" $false }
                for ($shapeIndex=1; $shapeIndex -le $sourceSlide.Shapes.Count; $shapeIndex++) { $sourceItems += Get-TextFrames $sourceSlide.Shapes.Item($shapeIndex) "s$shapeIndex" $false }
                # Generic fallback: when the master exposes no usable footer
                # geometry, use the lowest source text/table content as the
                # page's immutable content boundary.
                $effectiveBottom = [math]::Max(1.0, $globalSourceBottom - 4)
                $processedTables = @{}
                foreach ($item in $outItems) {
                  $sourceItem = $sourceItems | Where-Object { $_.Path -eq $item.Path } | Select-Object -First 1
                  $shape = $item.Shape; $actions = @()
                  if ($null -eq $sourceItem) { $records.Add([pscustomobject]@{slide=$slideIndex; object=$item.Path; status='UNSUPPORTED_MAPPING'; actions=@(); font_size=$null}); continue }
                  if ($item.InTable) {
                    $tableKey = "$slideIndex|$($item.Path -replace '/t.*$','')"
                    if (-not $processedTables.ContainsKey($tableKey)) { Fit-TableMeasured $item.Container $sourceItem.Container $MinimumFont $slide $effectiveBottom; $processedTables[$tableKey] = $true }
                    $tableStatus = if (Is-Overflow $shape $slide $false) { 'UNRESOLVED_TABLE_OVERFLOW' } else { 'FIT' }
                    $records.Add([pscustomobject]@{slide=$slideIndex; object=$item.Path; status=$tableStatus; actions=@('table-uniform-measured-fit'); font_size=$shape.TextFrame2.TextRange.Font.Size})
                    continue
                  }
                  # Translation writes only a:t values, so geometry remains the
                  # original geometry.  Table cells and group children reject
                  # direct geometry assignment through the PowerPoint COM API.
                  # Typeface is deliberately left intact because the translation
                  # font policy owns it.
                  $shape.TextFrame2.AutoSize = $msoAutoSizeNone
                  $sourceSize = $sourceItem.Shape.TextFrame2.TextRange.Font.Size
                  if ($sourceSize -gt 0) { $shape.TextFrame2.TextRange.Font.Size = $sourceSize }
                  if (-not (Is-Overflow $shape $slide)) { $records.Add([pscustomobject]@{slide=$slideIndex; object=$item.Path; status='FIT'; actions=@('preserve'); font_size=$sourceSize}); continue }
                  # Width-sensitive Latin strategy: keep the original size and
                  # try Arial Narrow before any fitting or size reduction.  The
                  # decision is based on PowerPoint's measured bounds, not on
                  # character-count thresholds.
                  $frameText = $shape.TextFrame2.TextRange.Text
                  if (($frameText -match '[A-Za-z]') -and ($frameText -notmatch '[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]')) {
                    $shape.TextFrame2.TextRange.Font.Name = 'Arial Narrow'
                    if (-not (Is-Overflow $shape $slide)) { $records.Add([pscustomobject]@{slide=$slideIndex; object=$item.Path; status='FIT'; actions=@('switch-arial-narrow'); font_size=$sourceSize}); continue }
                  }
                  $isStandaloneBody = (($shape.Type -eq 17) -and ($sourceItem.Shape.Height -ge 72) -and ($item.Path -notmatch '/g'))
                  if ($isStandaloneBody -and (Is-Overflow $shape $slide) -and (Expand-BodyHeightSafely $shape $slide $effectiveBottom)) { $actions += 'expand-body-height' }
                  elseif ($isStandaloneBody) { $actions += 'source-content-boundary' }
                  if (Is-Overflow $shape $slide) {
                    $tf = $shape.TextFrame2
                    # Force wrapping before any size reduction.  A number of
                    # source title boxes have WordWrap disabled; without this
                    # PowerPoint reports horizontal overflow even though the
                    # text can fit on additional lines inside the box.
                    $tf.WordWrap = $msoTrue
                    $tf.MarginLeft = [math]::Min($tf.MarginLeft, 1); $tf.MarginRight = [math]::Min($tf.MarginRight, 1)
                    $tf.MarginTop = [math]::Min($tf.MarginTop, 1); $tf.MarginBottom = [math]::Min($tf.MarginBottom, 1)
                    $shape.TextFrame.TextRange.ParagraphFormat.SpaceBefore = 0; $shape.TextFrame.TextRange.ParagraphFormat.SpaceAfter = 0
                    $actions += 'compact-safe'
                  }
                  if (Is-Overflow $shape $slide) {
                    $shape.TextFrame2.AutoSize = $msoAutoSizeTextToFitShape; $actions += 'powerpoint-autofit'
                    Fit-Measured $shape $slide $MinimumFont; $actions += 'measured-font-fit'
                  }
                  $actualSize = $shape.TextFrame2.TextRange.Font.Size
                  if ($actualSize -gt 0 -and $actualSize -lt $MinimumFont) {
                    $shape.TextFrame2.AutoSize = $msoAutoSizeNone; $shape.TextFrame2.TextRange.Font.Size = $MinimumFont; $actions += 'minimum-font-enforced'
                  }
                  $status = if (Is-Overflow $shape $slide) { 'UNRESOLVED_OVERFLOW' } else { 'FIT' }
                  $records.Add([pscustomobject]@{slide=$slideIndex; object=$item.Path; status=$status; actions=$actions; font_size=$actualSize})
                }
              }
              # PowerPoint writes normAutofit only when a fixed-size shape is
              # saved.  Reopen before deciding whether an object is deliverable.
              $targetPresentation.Save(); $targetPresentation.Close(); $targetPresentation = $app.Presentations.Open($Target, $msoFalse, $msoFalse, $msoFalse)
              $recordsByKey = @{}
              foreach ($record in $records) { $recordsByKey["$($record.slide)|$($record.object)"] = $record }
              for ($slideIndex=1; $slideIndex -le $targetPresentation.Slides.Count; $slideIndex++) {
                $slide = $targetPresentation.Slides.Item($slideIndex); $outItems = @()
                for ($shapeIndex=1; $shapeIndex -le $slide.Shapes.Count; $shapeIndex++) { $outItems += Get-TextFrames $slide.Shapes.Item($shapeIndex) "s$shapeIndex" $false }
                foreach ($item in $outItems) {
                  $record = $recordsByKey["$slideIndex|$($item.Path)"]
                  if ($null -eq $record -or $record.status -eq 'UNSUPPORTED_MAPPING') { continue }
                  $record.font_size = $item.Shape.TextFrame2.TextRange.Font.Size
                  $checkPageBounds = -not $item.InTable
                  if (Is-Overflow $item.Shape $slide $checkPageBounds) {
                    $record.status = if ($item.InTable) { 'UNRESOLVED_TABLE_OVERFLOW' } else { 'UNRESOLVED_OVERFLOW' }
                  } else { $record.status = 'FIT' }
                }
              }
              $targetPresentation.Save()
            } finally {
              if ($targetPresentation) { $targetPresentation.Close() }; if ($sourcePresentation) { $sourcePresentation.Close() }; if ($app) { $app.Quit() }
            }
            $unresolved = @($records | Where-Object { $_.status -ne 'FIT' }).Count
            [pscustomobject]@{ reviewed=$records.Count; unresolved=$unresolved; records=$records } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $Report -Encoding UTF8
        ''').strip()
