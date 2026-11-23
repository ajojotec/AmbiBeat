"""OpenGL support for the Qt edge-lighting renderer."""

import math
import struct

from PySide6.QtGui import (
    QGuiApplication,
    QOffscreenSurface,
    QOpenGLContext,
    QVector2D,
    QVector4D,
)
from PySide6.QtOpenGL import (
    QOpenGLBuffer,
    QOpenGLShader,
    QOpenGLShaderProgram,
    QOpenGLVertexArrayObject,
)


VERTEX_SHADER = """
attribute vec2 position;
varying vec2 point;
void main() {
    point = (position + 1.0) * 0.5;
    gl_Position = vec4(position, 0.0, 1.0);
}
"""

FRAGMENT_SHADER = """
#ifdef GL_ES
precision highp float;
#endif
varying vec2 point;
uniform vec2 size;
uniform float angle;
uniform float fade;
uniform float depth;
uniform float opacity;
uniform int colorCount;
uniform vec4 colors[16];
void main() {
    vec2 p = vec2(point.x, 1.0 - point.y);
    vec2 centered = (p - vec2(0.5)) * 2.0;
    float theta = atan(-centered.y * size.y, centered.x * size.x);
    float t = fract((theta - angle) / 6.28318530718);
    float stepIndex = t * float(colorCount);
    int a = int(floor(stepIndex));
    int b = a + 1;
    if (b >= colorCount) b = 0;
    vec4 color = mix(colors[a], colors[b], fract(stepIndex));
    float inner = max(0.01, 1.0 - (0.32 + fade * 0.33) * depth);
    float distanceFromCenter = length(centered * size) / min(size.x, size.y);
    float mask = clamp((distanceFromCenter - inner) / (1.0 - inner), 0.0, 1.0);
    gl_FragColor = vec4(color.rgb, opacity * mask);
}
"""


class BorderShader:
    def __init__(self):
        self.program = QOpenGLShaderProgram()
        for kind, source in (
            (QOpenGLShader.ShaderTypeBit.Vertex, VERTEX_SHADER),
            (QOpenGLShader.ShaderTypeBit.Fragment, FRAGMENT_SHADER),
        ):
            if not self.program.addShaderFromSourceCode(kind, source):
                raise RuntimeError("OpenGL border shader compilation failed: " + self.program.log())
        if not self.program.link():
            raise RuntimeError("OpenGL border shader linking failed: " + self.program.log())
        self.vao = QOpenGLVertexArrayObject()
        self.buffer = QOpenGLBuffer()
        if not self.vao.create() or not self.buffer.create():
            raise RuntimeError("OpenGL border buffers could not be created")
        self.vao.bind()
        if not self.buffer.bind():
            raise RuntimeError("OpenGL border buffer could not be bound")
        vertices = struct.pack("8f", -1, -1, 1, -1, -1, 1, 1, 1)
        self.buffer.allocate(vertices, len(vertices))
        if not self.program.bind():
            raise RuntimeError("OpenGL border shader could not be bound")
        location = self.program.attributeLocation("position")
        self.program.enableAttributeArray(location)
        self.program.setAttributeBuffer(location, 0x1406, 0, 2)
        self.program.release()
        self.buffer.release()
        self.vao.release()

    def draw(self, width, height, ratio, border, state):
        palette = border.border_palette(state)
        opacity = palette[0].alphaF()
        functions = QOpenGLContext.currentContext().functions()
        functions.glViewport(0, 0, round(width * ratio), round(height * ratio))
        functions.glDisable(0x0C11)  # GL_SCISSOR_TEST
        functions.glDisable(0x0B90)  # GL_STENCIL_TEST
        functions.glDisable(0x0B71)  # GL_DEPTH_TEST
        functions.glDisable(0x0B44)  # GL_CULL_FACE
        functions.glColorMask(True, True, True, True)
        functions.glEnable(0x0BE2)  # GL_BLEND
        functions.glBlendFuncSeparate(0x0302, 0x0303, 1, 0x0303)
        if not self.program.bind():
            raise RuntimeError("OpenGL border shader could not be bound")
        self.vao.bind()
        self.program.setUniformValue("size", QVector2D(width, height))
        angle = border.angle if state.get("edgeSwirl") else 0
        functions.glUniform1f(self.program.uniformLocation("angle"), math.radians(angle))
        functions.glUniform1f(self.program.uniformLocation("fade"), float(state.get("edgeFade", 50)) / 100)
        functions.glUniform1f(self.program.uniformLocation("depth"), float(state.get("edgeDepth", 100)) / 100)
        functions.glUniform1f(self.program.uniformLocation("opacity"), opacity)
        functions.glUniform1i(self.program.uniformLocation("colorCount"), len(palette))
        for index, color in enumerate(palette):
            self.program.setUniformValue(
                f"colors[{index}]", QVector4D(color.redF(), color.greenF(), color.blueF(), 1)
            )
        functions.glDrawArrays(0x0005, 0, 4)  # GL_TRIANGLE_STRIP
        self.vao.release()
        self.program.release()


def gpu_available():
    if QGuiApplication.platformName() in ("offscreen", "minimal"):
        return False, "the Qt platform does not support OpenGL widgets"
    context = QOpenGLContext()
    if not context.create():
        return False, "could not create an OpenGL context"
    surface = QOffscreenSurface()
    surface.setFormat(context.format())
    surface.create()
    if not surface.isValid() or not context.makeCurrent(surface):
        return False, "could not make an OpenGL context current"
    try:
        renderer = context.functions().glGetString(0x1F01) or ""
        if any(name in renderer.lower() for name in ("llvmpipe", "softpipe", "swiftshader", "software")):
            return False, f"software OpenGL renderer: {renderer}"
        try:
            shader = BorderShader()
        except RuntimeError as error:
            return False, str(error)
        del shader
        return True, renderer
    finally:
        context.doneCurrent()
