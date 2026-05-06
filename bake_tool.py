from typing import Optional
import bpy
from mathutils import Vector
import numpy as np
from mathutils.bvhtree import BVHTree
import multiprocessing as mp
from concurrent.futures import ThreadPoolExecutor
import os

# 常量定义
TEMP_COLLISION_MESH_NAME = "TempCollisionMesh"
MAX_THREADS = max(1, mp.cpu_count() - 1)  # 保留一个核心给系统

ATTRIBUTE_TANGENT = "Tangent"
ATTRIBUTE_COEFFS_0 = "SH0"
ATTRIBUTE_COEFFS_1 = "SH1"

PI_2 = 2 * np.pi  

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
        self.mesh.calc_tangents()

        # 获取顶点数据
        self.vertices = np.array([v.co for v in self.mesh.vertices], dtype=np.float32)
        self.normals = np.array([v.normal for v in self.mesh.vertices], dtype=np.float32)
        if self.args["reverse_normal"]:
            self.normals = -self.normals
        self.tangents, self.bitangents = self._compute_tangent_space()

        # 创建临时碰撞对象
        self.temp_obj = self._create_temp_collision_object()
        
        # 创建BVH树
        self.bvh = BVHTree.FromObject(self.temp_obj, self.context.evaluated_depsgraph_get())
        
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
            
        # 创建临时对象
        temp_collision_mesh = bpy.data.meshes.new(TEMP_COLLISION_MESH_NAME)
        temp_obj = bpy.data.objects.new("TempCollisionObj", temp_collision_mesh)
        bpy.context.scene.collection.objects.link(temp_obj)

        # 复制并合并对象
        copied_objects = []
        copied_mesh_names = []
        for obj in temp_mesh_objects:
            obj_copy = obj.copy()
            obj_copy.data = obj.data.copy()
            copied_objects.append(obj_copy)
            copied_mesh_names.append(obj_copy.data.name)
            bpy.context.scene.collection.objects.link(obj_copy)
        
        # 设置选择状态并合并
        original_active = self.context.view_layer.objects.active
        self.mesh_obj.select_set(False)
        for obj in copied_objects:
            obj.select_set(True)
        temp_obj.select_set(True)
        self.context.view_layer.objects.active = temp_obj
        
        bpy.ops.object.join()

        # 恢复选择状态
        temp_obj.select_set(False)
        self.mesh_obj.select_set(True)
        self.context.view_layer.objects.active = original_active
        
        # 清理临时mesh
        for name in copied_mesh_names:
            if name in bpy.data.meshes:
                bpy.data.meshes.remove(bpy.data.meshes[name], do_unlink=True)
        
        return temp_obj

    def _compute_bent_normals(self,) -> None:
        """计算弯曲法线和AO"""
        if not self.temp_obj:
            return
        sh_coeffs = self._compute_sh_coefficients_parallel()
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
        def compute_orthogonal(normal: np.ndarray) -> np.ndarray:
            ref = np.array([1.0, 0.0, 0.0]) if abs(normal[1]) > abs(normal[0]) else np.array([0.0, 1.0, 0.0])
            tangent = np.cross(normal, ref)
            return tangent / np.linalg.norm(tangent)
        
        tangents = np.array([compute_orthogonal(n) for n in self.normals], dtype=np.float32)
        bitangents = np.cross(self.normals, tangents)
        return tangents, bitangents

    def _compute_sh_coefficients_parallel(self,) -> np.ndarray:
        """并行计算球谐系数"""
        num_vertices = len(self.vertices)
        batch_size = max(1, num_vertices // MAX_THREADS)
        
        # 创建任务列表
        tasks = []
        for i in range(0, num_vertices, batch_size):
            end_idx = min(i + batch_size, num_vertices)
            tasks.append((i, end_idx))
        
        with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
            results = list(executor.map(lambda args: self._process_vertex_batch(*args), tasks))
        
        # 合并结果
        sh_coeffs = np.concatenate(results)
        return sh_coeffs / len(self.sample_dirs)
    
    def _process_vertex_batch(self, start_idx:int, end_idx:int,) -> np.ndarray:
        """处理一批顶点的计算"""
        sh_coeffs = np.zeros((end_idx - start_idx, 4), dtype=np.float32)
        ray_starts = self.vertices[start_idx:end_idx] + self.normals[start_idx:end_idx] * self.args["ray_offset"]
        
        for i in range(end_idx - start_idx):
            dirs = (self.tangents[start_idx + i] * self.sample_dirs[:, 0:1] + 
                self.bitangents[start_idx + i] * self.sample_dirs[:, 1:2] + 
                self.normals[start_idx + i] * self.sample_dirs[:, 2:3])
            
            for dir in dirs:
                _, _, index, distance = self.bvh.ray_cast(
                    Vector(ray_starts[i]),
                    Vector(dir),
                    self.args["max_distance"]
                )
                
                # 计算球谐基函数
                y00 = Y00_COEFF  # Y00系数
                y10 = Y10_COEFF * dir[2]  # Y10系数
                y11 = Y11_COEFF * dir[0]  # Y11系数
                y1_1 = Y1_1_COEFF * dir[1]  # Y1-1系数
                
                if index is None:
                    weight = 1.0
                else:
                    weight = min(self.args["max_distance"], distance / self.args["max_distance"])
                
                # 累加球谐系数
                sh_coeffs[i][0] += y00 * weight  # Y00
                sh_coeffs[i][1] += y10 * weight  # Y10
                sh_coeffs[i][2] += y11 * weight  # Y11
                sh_coeffs[i][3] += y1_1 * weight # Y1-1
        
        return sh_coeffs

    def _store_results(self, sh_coeffs: np.ndarray) -> None:
        """存储计算结果"""
        # 确保属性存在
        self._ensure_attribute(ATTRIBUTE_COEFFS_0, 'FLOAT', 'POINT')
        self._ensure_attribute(ATTRIBUTE_COEFFS_1, 'FLOAT_VECTOR', 'CORNER')
        self._ensure_attribute(ATTRIBUTE_TANGENT, 'FLOAT_VECTOR', 'CORNER')
        # self._ensure_attribute(ATTRIBUTE_TANGENT, 'FLOAT_VECTOR', 'POINT')

        # 获取属性
        sh0_attr = self.mesh.attributes[ATTRIBUTE_COEFFS_0]
        sh1_attr = self.mesh.attributes[ATTRIBUTE_COEFFS_1]
        tangent_attr = self.mesh.attributes[ATTRIBUTE_TANGENT]
        
        # 保存Y00系数
        for vert in self.mesh.vertices:
            sh0_attr.data[vert.index].value = sh_coeffs[vert.index][0]
        
        for loop in self.mesh.loops:
            # 保存BentNormal Y10, Y11, Y1-1系数
            # 这里调整了位置与正负，对应xyz坐标系中的弯曲法线
            sh1_attr.data[loop.index].vector = Vector((
                -sh_coeffs[loop.vertex_index][2],  # Y11
                sh_coeffs[loop.vertex_index][3],   # Y1-1
                sh_coeffs[loop.vertex_index][1]    # Y10
            ))
            # 保存Tangent
            tangent_attr.data[loop.index].vector = self.mesh.loops[loop.index].tangent
            # tangent_attr.data[loop.vertex_index].vector = self.mesh.loops[loop.index].tangent

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
    # 获取图像像素数据
    width, height = image.size
    pixels = np.array(image.pixels).reshape(height, width, 4)
    
    # 重新采样到 256x128
    target_height, target_width = 128, 256
    h_indices = np.linspace(0, height-1, target_height).astype(int)
    w_indices = np.linspace(0, width-1, target_width).astype(int)
    pixels = pixels[h_indices][:, w_indices]
    
    height, width = target_height, target_width
    
    # 转换为球面坐标
    theta = np.linspace(0, np.pi, height)
    phi = np.linspace(0, 2 * np.pi, width)
    theta_grid, phi_grid = np.meshgrid(theta, phi, indexing='ij')
    
    # 计算球谐基函数
    sh_coeffs = np.zeros((9, 3), dtype=np.float32)  # 改为存储RGB三个通道
    
    # 遍历所有像素
    for i in range(height):
        for j in range(width):
            # 获取当前像素的颜色值
            color = pixels[i, j, :3]  # 直接使用RGB颜色值
            
            # 计算球谐基函数值
            theta = theta_grid[i, j]
            phi = phi_grid[i, j]
            x = np.sin(theta) * np.cos(phi)
            y = np.sin(theta) * np.sin(phi)
            z = np.cos(theta)
            
            # 计算球谐基函数
            sh_basis = np.array([
                Y00_COEFF,  # Y00
                Y1_1_COEFF * y,  # Y1-1
                Y10_COEFF * z,  # Y10
                Y11_COEFF * x,  # Y11
                Y2_2_COEFF * (x * y),  # Y2-2
                Y2_1_COEFF * (y * z),  # Y2-1
                Y20_COEFF * (3 * z * z - 1) / 2,  # Y20
                Y21_COEFF * (x * z),  # Y21
                Y22_COEFF * (x * x - y * y) / 2  # Y22
            ])
            
            # 添加sin(theta)作为积分权重
            weight = np.sin(theta) * (2 * np.pi / width) * (np.pi / height)
            sh_coeffs += sh_basis[:, np.newaxis] * color * weight

    # 将球谐系数转换为Vector数组
    sh_coeffs = [
        Vector(sh_coeffs[0]),  # Y00
        Vector(sh_coeffs[1]),  # Y1-1
        Vector(sh_coeffs[2]),  # Y10
        Vector(sh_coeffs[3]),  # Y11
        Vector(sh_coeffs[4]),  # Y2-2
        Vector(sh_coeffs[5]),  # Y2-1
        Vector(sh_coeffs[6]),  # Y20
        Vector(sh_coeffs[7]),  # Y21
        Vector(sh_coeffs[8])   # Y22
    ]
    return sh_coeffs

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

    nodes = irradiance_node.nodes
    def get_node_by_label(label):
        for node in nodes:
            if node.label == label:
                return node
        return None
    
    def assign_vector_values(node, vector):
        node.inputs[0].default_value = vector[0]
        node.inputs[1].default_value = vector[1]
        node.inputs[2].default_value = vector[2]

    sh_coeffs = image_to_sh_coeffs(image)
    mat_node_names = ["y00", "y1_1", "y10", "y11", "y2_2", "y2_1", "y20", "y21", "y22"]
    debug_value_names = ["TEST_IRR_Y00", "TEST_IRR_Y1_1", "TEST_IRR_Y10", "TEST_IRR_Y11", "TEST_IRR_Y2_2", "TEST_IRR_Y2_1", "TEST_IRR_Y20", "TEST_IRR_Y21", "TEST_IRR_Y22"]
    
    for i, name in enumerate(mat_node_names):
        coeffs = sh_coeffs[i]
        node = get_node_by_label(name)
        if node:
            assign_vector_values(node, coeffs)

        print(f"#define {debug_value_names[i]}  float3({coeffs[0]}, {coeffs[1]}, {coeffs[2]})")

    return True