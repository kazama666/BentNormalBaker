from typing import Optional
import bpy
from mathutils import Vector
import numpy as np
from mathutils.bvhtree import BVHTree
import os

# 常量定义
TEMP_COLLISION_MESH_NAME = "TempCollisionMesh"

ATTRIBUTE_TANGENT = "Tangent"
ATTRIBUTE_COEFFS_0 = "SH0"
ATTRIBUTE_COEFFS_1 = "SH1"

PI_2 = 2 * np.pi
_IRRADIANCE_SH_CACHE = {}

# 一阶球谐基函数常量系数
Y00_COEFF = 0.5 * np.sqrt(1.0 / np.pi)
Y1_1_COEFF = 0.5 * np.sqrt(3.0 / PI_2)
Y10_COEFF = 0.5 * np.sqrt(3.0 / np.pi)
Y11_COEFF = -0.5 * np.sqrt(3.0 / PI_2)


# 二阶球谐基函数常量系数
Y2_2_COEFF = 0.25 * np.sqrt(15.0 / PI_2)
Y2_1_COEFF = 0.5 * np.sqrt(15.0 / PI_2)
Y20_COEFF = 0.25 * np.sqrt(5.0 / np.pi)
Y21_COEFF = -0.5 * np.sqrt(15.0 / PI_2)
Y22_COEFF = 0.25 * np.sqrt(15.0 / PI_2)

class Baker:
    """
    将顶点周围各方向的遮挡信息积分到一阶球谐系数
        sh_coeffs_y00: 顶点Attribute -> SH_0
        sh_coeffs_y11: 顶点Attribute -> -SH_1.x
        sh_coeffs_y1_1: 顶点Attribute -> SH_1.y
        sh_coeffs_y10: 顶点Attribute -> SH_1.z
    """
    def __init__(self, context: bpy.types.Context, mesh_obj: bpy.types.Object, args: dict):
        """初始化烘焙器
        Args:
            context: Blender上下文
            mesh_obj: 要烘焙的网格对象
            args: 烘焙参数
        """
        self.context = context
        self.mesh_obj = mesh_obj
        self.mesh = mesh_obj.data
        self.args = args

        # 初始化网格数据
        self.mesh.calc_loop_triangles()

        # 获取顶点数据
        vertex_count = len(self.mesh.vertices)
        self.vertices = np.empty(vertex_count * 3, dtype=np.float32)
        self.normals = np.empty(vertex_count * 3, dtype=np.float32)
        self.mesh.vertices.foreach_get("co", self.vertices)
        self.mesh.vertices.foreach_get("normal", self.normals)
        self.vertices = self.vertices.reshape(vertex_count, 3)
        self.normals = self.normals.reshape(vertex_count, 3)
        if self.args["reverse_normal"]:
            self.normals = -self.normals
        self.tangents, self.bitangents = self._compute_tangent_space()

        # 创建临时碰撞对象
        self.temp_obj = self._create_temp_collision_object()

        # 创建BVH树
        self.bvh = BVHTree.FromObject(self.temp_obj, self.context.evaluated_depsgraph_get()) if self.temp_obj else None

        # 计算采样方向
        self.sample_dirs = self._generate_sample_directions()

    def bake(self,) -> None:
        """
        计算网格的弯曲法线和环境光遮蔽
        """
        try:
            # 计算弯曲法线和AO
            self._compute_bent_normals()
        finally:
            # 清理临时对象
            self._cleanup_temp_objects()

    def _ensure_attribute(self, name: str, type: str, domain: str) -> None:
        """确保属性存在"""
        if name not in self.mesh.attributes:
            self.mesh.attributes.new(name=name, type=type, domain=domain)

    def _create_temp_collision_object(self) -> Optional[bpy.types.Object]:
        """创建临时碰撞对象"""
        if self.args["collection_name"] not in bpy.data.collections:
            return None

        temp_mesh_objects = [obj for obj in bpy.data.collections[self.args["collection_name"]].objects if obj.type == 'MESH']
        if not temp_mesh_objects:
            return None

        vertices = []
        faces = []
        depsgraph = self.context.evaluated_depsgraph_get()
        target_matrix_inverted = self.mesh_obj.matrix_world.inverted()

        for obj in temp_mesh_objects:
            evaluated_obj = obj.evaluated_get(depsgraph)
            mesh = evaluated_obj.to_mesh()
            if mesh is None:
                continue

            try:
                vertex_offset = len(vertices)
                matrix = target_matrix_inverted @ evaluated_obj.matrix_world
                vertices.extend(matrix @ vertex.co for vertex in mesh.vertices)
                faces.extend(tuple(vertex_offset + index for index in polygon.vertices) for polygon in mesh.polygons)
            finally:
                evaluated_obj.to_mesh_clear()

        if not vertices or not faces:
            return None

        temp_collision_mesh = bpy.data.meshes.new(TEMP_COLLISION_MESH_NAME)
        temp_collision_mesh.from_pydata(vertices, [], faces)
        temp_collision_mesh.update()

        temp_obj = bpy.data.objects.new("TempCollisionObj", temp_collision_mesh)
        bpy.context.scene.collection.objects.link(temp_obj)
        return temp_obj

    def _compute_bent_normals(self,) -> None:
        """计算弯曲法线和AO"""
        if self.bvh is None:
            sh_coeffs = self._compute_unoccluded_sh_coefficients()
        else:
            sh_coeffs = self._compute_sh_coefficients()
        self._store_results(sh_coeffs)

    def _generate_sample_directions(self) -> np.ndarray:
        """在法线方向的锥形角度范围内生成均匀采样方向

        Args:
            sample_count: 采样数量
            sample_angle: 最大采样角度（度）
        """
        sample_angle_rad = np.radians(self.args["sample_angle"])
        points = []

        for i in range(self.args["sample_count"]):
            # 生成均匀分布的u和v
            u = 0.0
            p = 0.5
            k = i
            while k > 0:
                if k & 1:
                    u += p
                p *= 0.5
                k >>= 1
            v = (i + 0.5) / self.args["sample_count"]

            # 计算角度范围
            theta = sample_angle_rad * np.sqrt(u)  # 使用sqrt(u)使采样在角度范围内均匀分布
            phi = 2 * np.pi * v

            # 转换为笛卡尔坐标
            x = np.sin(theta) * np.cos(phi)
            y = np.sin(theta) * np.sin(phi)
            z = np.cos(theta)

            points.append([x, y, z])

        return np.array(points, dtype=np.float32)

    def _compute_tangent_space(self,) -> tuple[np.ndarray, np.ndarray]:
        """计算切线空间"""
        refs = np.zeros_like(self.normals)
        use_x = np.abs(self.normals[:, 1]) > np.abs(self.normals[:, 0])
        refs[use_x, 0] = 1.0
        refs[~use_x, 1] = 1.0

        tangents = np.cross(self.normals, refs)
        lengths = np.linalg.norm(tangents, axis=1, keepdims=True)
        tangents = tangents / lengths
        bitangents = np.cross(self.normals, tangents)
        return tangents.astype(np.float32), bitangents.astype(np.float32)

    def _compute_unoccluded_sh_coefficients(self,) -> np.ndarray:
        """计算无遮挡球谐系数"""
        num_vertices = len(self.vertices)
        sh_coeffs = np.zeros((num_vertices, 4), dtype=np.float32)

        for i in range(num_vertices):
            dirs = (self.tangents[i] * self.sample_dirs[:, 0:1] +
                self.bitangents[i] * self.sample_dirs[:, 1:2] +
                self.normals[i] * self.sample_dirs[:, 2:3])
            sh_coeffs[i, 0] = Y00_COEFF
            sh_coeffs[i, 1] = np.sum(Y10_COEFF * dirs[:, 2]) / len(self.sample_dirs)
            sh_coeffs[i, 2] = np.sum(Y11_COEFF * dirs[:, 0]) / len(self.sample_dirs)
            sh_coeffs[i, 3] = np.sum(Y1_1_COEFF * dirs[:, 1]) / len(self.sample_dirs)

        return sh_coeffs

    def _compute_sh_coefficients(self,) -> np.ndarray:
        """计算球谐系数"""
        return self._process_vertex_batch(0, len(self.vertices))

    def _process_vertex_batch(self, start_idx:int, end_idx:int,) -> np.ndarray:
        """处理一批顶点的计算"""
        sh_coeffs = np.zeros((end_idx - start_idx, 4), dtype=np.float32)
        vertices = self.vertices
        normals = self.normals
        tangents = self.tangents
        bitangents = self.bitangents
        sample_dirs = self.sample_dirs
        sample_count = len(sample_dirs)
        inv_sample_count = 1.0 / sample_count
        ray_offset = self.args["ray_offset"]
        max_distance = self.args["max_distance"]
        ray_cast = self.bvh.ray_cast
        ray_starts = vertices[start_idx:end_idx] + normals[start_idx:end_idx] * ray_offset
        weights = np.empty(sample_count, dtype=np.float32)

        for i in range(end_idx - start_idx):
            vertex_index = start_idx + i
            vertex_dirs = (tangents[vertex_index] * sample_dirs[:, 0:1] +
                bitangents[vertex_index] * sample_dirs[:, 1:2] +
                normals[vertex_index] * sample_dirs[:, 2:3])

            ray_start = Vector(ray_starts[i])
            direction = Vector((0.0, 0.0, 0.0))

            for sample_index in range(sample_count):
                sample_direction = vertex_dirs[sample_index]
                direction[0] = sample_direction[0]
                direction[1] = sample_direction[1]
                direction[2] = sample_direction[2]

                _, _, index, distance = ray_cast(ray_start, direction, max_distance)
                weights[sample_index] = 1.0 if index is None else min(max_distance, distance / max_distance)

            sh_coeffs[i] = (
                Y00_COEFF * np.sum(weights),
                Y10_COEFF * np.dot(vertex_dirs[:, 2], weights),
                Y11_COEFF * np.dot(vertex_dirs[:, 0], weights),
                Y1_1_COEFF * np.dot(vertex_dirs[:, 1], weights),
            )

        return sh_coeffs * inv_sample_count

    def _store_results(self, sh_coeffs: np.ndarray) -> None:
        """存储计算结果"""
        # 确保属性存在
        self._ensure_attribute(ATTRIBUTE_COEFFS_0, 'FLOAT', 'POINT')
        self._ensure_attribute(ATTRIBUTE_COEFFS_1, 'FLOAT_VECTOR', 'CORNER')
        self._ensure_attribute(ATTRIBUTE_TANGENT, 'FLOAT_VECTOR', 'CORNER')

        # 获取属性
        sh0_attr = self.mesh.attributes[ATTRIBUTE_COEFFS_0]
        sh1_attr = self.mesh.attributes[ATTRIBUTE_COEFFS_1]
        tangent_attr = self.mesh.attributes[ATTRIBUTE_TANGENT]

        sh0_attr.data.foreach_set("value", sh_coeffs[:, 0])

        loop_count = len(self.mesh.loops)
        loop_vertex_indices = np.empty(loop_count, dtype=np.int32)
        self.mesh.loops.foreach_get("vertex_index", loop_vertex_indices)

        sh1_values = np.empty((loop_count, 3), dtype=np.float32)
        loop_coeffs = sh_coeffs[loop_vertex_indices]
        sh1_values[:, 0] = -loop_coeffs[:, 2]
        sh1_values[:, 1] = loop_coeffs[:, 3]
        sh1_values[:, 2] = loop_coeffs[:, 1]
        sh1_attr.data.foreach_set("vector", sh1_values.ravel())

        tangent_values = self.tangents[loop_vertex_indices]
        tangent_attr.data.foreach_set("vector", tangent_values.ravel())
        self.mesh.update()

    def _cleanup_temp_objects(self) -> None:
            """清理临时对象"""
            if self.temp_obj:
                bpy.data.objects.remove(self.temp_obj, do_unlink=True)
                if bpy.data.meshes.get(TEMP_COLLISION_MESH_NAME):
                    bpy.data.meshes.remove(bpy.data.meshes[TEMP_COLLISION_MESH_NAME], do_unlink=True)

def clear_baked_attributes(mesh_obj: bpy.types.Object):
    """清除已烘焙的属性"""
    mesh = mesh_obj.data
    if ATTRIBUTE_COEFFS_0 in mesh.attributes:
        mesh.attributes.remove(mesh.attributes[ATTRIBUTE_COEFFS_0])
    if ATTRIBUTE_COEFFS_1 in mesh.attributes:
        mesh.attributes.remove(mesh.attributes[ATTRIBUTE_COEFFS_1])
    if ATTRIBUTE_TANGENT in mesh.attributes:
        mesh.attributes.remove(mesh.attributes[ATTRIBUTE_TANGENT])

def image_to_sh_coeffs(image: bpy.types.Image) -> list[Vector]:
    """
    将图像转换为二阶球谐系数
    在DebugShader中使用
    Args:
        image: 输入的HDR图像
    Returns:
        list[Vector]: 9个二阶球谐系数 (Y00, Y1-1, Y10, Y11, Y2-2, Y2-1, Y20, Y21, Y22)
    """
    width, height = image.size
    pixels = np.empty(width * height * 4, dtype=np.float32)
    image.pixels.foreach_get(pixels)
    pixels = pixels.reshape(height, width, 4)

    target_height, target_width = 128, 256
    h_indices = np.linspace(0, height - 1, target_height).astype(int)
    w_indices = np.linspace(0, width - 1, target_width).astype(int)
    colors = pixels[np.ix_(h_indices, w_indices)][:, :, :3]

    theta = np.linspace(0, np.pi, target_height, dtype=np.float32)
    phi = np.linspace(0, 2 * np.pi, target_width, dtype=np.float32)
    theta_grid, phi_grid = np.meshgrid(theta, phi, indexing='ij')

    sin_theta = np.sin(theta_grid)
    x = sin_theta * np.cos(phi_grid)
    y = sin_theta * np.sin(phi_grid)
    z = np.cos(theta_grid)

    sh_basis = np.stack((
        np.full_like(x, Y00_COEFF),
        Y1_1_COEFF * y,
        Y10_COEFF * z,
        Y11_COEFF * x,
        Y2_2_COEFF * (x * y),
        Y2_1_COEFF * (y * z),
        Y20_COEFF * (3 * z * z - 1) / 2,
        Y21_COEFF * (x * z),
        Y22_COEFF * (x * x - y * y) / 2,
    ), axis=0)

    weights = sin_theta * (2 * np.pi / target_width) * (np.pi / target_height)
    sh_coeffs = np.einsum('khw,hwc,hw->kc', sh_basis, colors, weights, optimize=True)
    return [Vector(coeff) for coeff in sh_coeffs]

def bake_irradiance() -> bool:
    """
    将环境贴图烘焙到球谐系数
    给Debug Material的Irradiance使用
    """
    irradiance_node = bpy.data.node_groups["Sample_Irradiance"]
    if irradiance_node is None:
        return False

    image = bpy.data.images.get("debug_environment.exr")
    if image is None:
        image_path = os.path.join(os.path.dirname(__file__), "assets/texture/debug_environment.exr")
        image = bpy.data.images.load(image_path)

    if image is None:
        return False

    nodes_by_label = {node.label: node for node in irradiance_node.nodes}

    def assign_vector_values(node, vector):
        node.inputs[0].default_value = vector[0]
        node.inputs[1].default_value = vector[1]
        node.inputs[2].default_value = vector[2]

    cache_key = (image.name, tuple(image.size), image.filepath)
    sh_coeffs = _IRRADIANCE_SH_CACHE.get(cache_key)
    if sh_coeffs is None:
        sh_coeffs = image_to_sh_coeffs(image)
        _IRRADIANCE_SH_CACHE[cache_key] = sh_coeffs
    mat_node_names = ["y00", "y1_1", "y10", "y11", "y2_2", "y2_1", "y20", "y21", "y22"]
    debug_value_names = ["TEST_IRR_Y00", "TEST_IRR_Y1_1", "TEST_IRR_Y10", "TEST_IRR_Y11", "TEST_IRR_Y2_2", "TEST_IRR_Y2_1", "TEST_IRR_Y20", "TEST_IRR_Y21", "TEST_IRR_Y22"]

    for i, name in enumerate(mat_node_names):
        coeffs = sh_coeffs[i]
        node = nodes_by_label.get(name)
        if node:
            assign_vector_values(node, coeffs)

        print(f"#define {debug_value_names[i]}  float3({coeffs[0]}, {coeffs[1]}, {coeffs[2]})")

    return True
