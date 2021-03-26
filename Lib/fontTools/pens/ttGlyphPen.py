from array import array
from typing import Any, Optional, Tuple
from fontTools.misc.fixedTools import MAX_F2DOT14, floatToFixedToFloat
from fontTools.misc.loggingTools import LogMixin
from fontTools.pens.pointPen import AbstractPointPen
from fontTools.misc.roundTools import otRound
from fontTools.pens.basePen import LoggingPen
from fontTools.pens.transformPen import TransformPen, TransformPointPen
from fontTools.ttLib.ttFont import _TTGlyphSet
from fontTools.ttLib.tables import ttProgram
from fontTools.ttLib.tables._g_l_y_f import Glyph
from fontTools.ttLib.tables._g_l_y_f import GlyphComponent
from fontTools.ttLib.tables._g_l_y_f import GlyphCoordinates


__all__ = ["TTGlyphPen", "TTGlyphPointPen"]


class TTGlyphBasePen:
    """
    Pen used for drawing to a TrueType glyph.

    This pen can be used to construct or modify glyphs in a TrueType format
    font. After using the pen to draw, use the ``.glyph()`` method to retrieve
    a :py:class:`~._g_l_y_f.Glyph` object representing the glyph.
    """

    def __init__(
        self, glyphSet: _TTGlyphSet, handleOverflowingTransforms: bool = True
    ) -> None:
        """
        Construct a new pen.

        Args:
            glyphSet (ttLib.ttFont._TTGlyphSet): A glyphset object, used to resolve components.
            handleOverflowingTransforms (bool): See below.

        If ``handleOverflowingTransforms`` is True, the components' transform values
        are checked that they don't overflow the limits of a F2Dot14 number:
        -2.0 <= v < +2.0. If any transform value exceeds these, the composite
        glyph is decomposed.

        An exception to this rule is done for values that are very close to +2.0
        (both for consistency with the -2.0 case, and for the relative frequency
        these occur in real fonts). When almost +2.0 values occur (and all other
        values are within the range -2.0 <= x <= +2.0), they are clamped to the
        maximum positive value that can still be encoded as an F2Dot14: i.e.
        1.99993896484375.

        If False, no check is done and all components are translated unmodified
        into the glyf table, followed by an inevitable ``struct.error`` once an
        attempt is made to compile them.
        """
        self.glyphSet = glyphSet
        self.handleOverflowingTransforms = handleOverflowingTransforms
        self.init()

    def init(self) -> None:
        self.points = []
        self.endPts = []
        self.types = []
        self.components = []
        self.currentPath = None

    def addComponent(
        self,
        baseGlyphName: str,
        transformation: Tuple[float, float, float, float, float, float],
        identifier: Optional[str] = None,
        **kwargs: Any
    ) -> None:
        """
        Add a sub glyph.
        """
        self.components.append((baseGlyphName, transformation))

    def _buildComponents(self, componentFlags):
        if self.handleOverflowingTransforms:
            # we can't encode transform values > 2 or < -2 in F2Dot14,
            # so we must decompose the glyph if any transform exceeds these
            overflowing = any(
                s > 2 or s < -2
                for (glyphName, transformation) in self.components
                for s in transformation[:4]
            )
        components = []
        for glyphName, transformation in self.components:
            if glyphName not in self.glyphSet:
                self.log.warning(
                    "skipped non-existing component '%s'", glyphName
                )
                continue
            if self.points or (
                self.handleOverflowingTransforms and overflowing
            ):
                # can't have both coordinates and components, so decompose
                self._decompose(glyphName, transformation)
                continue

            component = GlyphComponent()
            component.glyphName = glyphName
            component.x, component.y = (otRound(v) for v in transformation[4:])
            # quantize floats to F2Dot14 so we get same values as when decompiled
            # from a binary glyf table
            transformation = tuple(
                floatToFixedToFloat(v, 14) for v in transformation[:4]
            )
            if transformation != (1, 0, 0, 1):
                if self.handleOverflowingTransforms and any(
                    MAX_F2DOT14 < s <= 2 for s in transformation
                ):
                    # clamp values ~= +2.0 so we can keep the component
                    transformation = tuple(
                        MAX_F2DOT14 if MAX_F2DOT14 < s <= 2 else s
                        for s in transformation
                    )
                component.transform = (transformation[:2], transformation[2:])
            component.flags = componentFlags
            components.append(component)
        return components

    def _glyph(self, componentFlags=0x4) -> Glyph:
        """
        Returns a :py:class:`~._g_l_y_f.Glyph` object representing the glyph.
        """
        components = self._buildComponents(componentFlags)

        glyph = Glyph()
        glyph.coordinates = GlyphCoordinates(self.points)
        glyph.coordinates.toInt()
        glyph.endPtsOfContours = self.endPts
        glyph.flags = array("B", self.types)
        self.init()

        if components:
            glyph.components = components
            glyph.numberOfContours = -1
        else:
            glyph.numberOfContours = len(glyph.endPtsOfContours)
            glyph.program = ttProgram.Program()
            glyph.program.fromBytecode(b"")

        return glyph


class TTGlyphPen(TTGlyphBasePen, LoggingPen):
    def _addPoint(self, pt: Tuple[float, float], onCurve: int) -> None:
        self.points.append(pt)
        self.types.append(onCurve)

    def _popPoint(self) -> None:
        self.points.pop()
        self.types.pop()

    def _isClosed(self) -> bool:
        return (not self.points) or (
            self.endPts and self.endPts[-1] == len(self.points) - 1
        )

    def _decompose(
        self,
        glyphName: str,
        transformation: Tuple[float, float, float, float, float, float],
    ):
        tpen = TransformPen(self, transformation)
        self.glyphSet[glyphName].draw(tpen)

    def lineTo(self, pt: Tuple[float, float]) -> None:
        self._addPoint(pt, 1)

    def moveTo(self, pt: Tuple[float, float]) -> None:
        assert self._isClosed(), '"move"-type point must begin a new contour.'
        self._addPoint(pt, 1)

    def curveTo(self, *points) -> None:
        raise NotImplementedError

    def qCurveTo(self, *points) -> None:
        assert len(points) >= 1
        for pt in points[:-1]:
            self._addPoint(pt, 0)

        # last point is None if there are no on-curve points
        if points[-1] is not None:
            self._addPoint(points[-1], 1)

    def closePath(self) -> None:
        endPt = len(self.points) - 1

        # ignore anchors (one-point paths)
        if endPt == 0 or (self.endPts and endPt == self.endPts[-1] + 1):
            self._popPoint()
            return

        # if first and last point on this path are the same, remove last
        startPt = 0
        if self.endPts:
            startPt = self.endPts[-1] + 1
        if self.points[startPt] == self.points[endPt]:
            self._popPoint()
            endPt -= 1

        self.endPts.append(endPt)

    def endPath(self) -> None:
        # TrueType contours are always "closed"
        self.closePath()

    def glyph(self, componentFlags: int = 0x4) -> Glyph:
        assert self._isClosed(), "Didn't close last contour."
        return self._glyph(componentFlags=componentFlags)


class TTGlyphPointPen(TTGlyphBasePen, LogMixin, AbstractPointPen):
    """
    Pen used for drawing to a TrueType glyph.

    This pen can be used to construct or modify glyphs in a TrueType format
    font. After using the pen to draw points, use the ``.glyph()`` method to
    retrieve a :py:class:`~._g_l_y_f.Glyph` object representing the glyph.
    """

    def _decompose(
        self,
        glyphName: str,
        transformation: Tuple[float, float, float, float, float, float],
    ):
        tpen = TransformPointPen(self, transformation)
        self.glyphSet[glyphName].drawPoints(tpen)

    def beginPath(
        self, identifier: Optional[str] = None, **kwargs: Any
    ) -> None:
        """
        Start a new sub path.
        """
        assert self.currentPath is None
        self.currentPath = []

    def endPath(self) -> None:
        """
        End the current sub path.
        """
        # TrueType contours are always "closed"
        assert self.currentPath is not None
        if self.currentPath:
            endPt = len(self.points) - 1
            self.endPts.append(endPt)
        self.currentPath = None

    def addPoint(
        self,
        pt: Tuple[float, float],
        segmentType: Optional[str] = None,
        smooth: bool = False,
        name: Optional[str] = None,
        identifier: Optional[str] = None,
        **kwargs: Any
    ) -> None:
        """
        Add a point to the current sub path.
        """
        assert self.currentPath is not None
        if segmentType is None:
            self.types.append(0)  # offcurve
        elif segmentType in ("qcurve", "line"):
            self.types.append(1)  # oncurve
        else:
            # cubic curves are not supported
            raise NotImplementedError

        self.currentPath.append(pt)
        self.points.append(pt)

    def glyph(self, componentFlags: int = 0x4) -> Glyph:
        """
        Returns a :py:class:`~._g_l_y_f.Glyph` object representing the glyph.
        """
        assert self.currentPath is None
        return self._glyph(componentFlags=componentFlags)
