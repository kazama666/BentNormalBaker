import bpy
from .bake_tool import Baker, clear_baked_attributes, bake_irradiance
from .node_tool import get_node, add_node_modifier, get_material, replace_material

class CachedMaterialPropertyGroup(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty(
        name="Material Name",
        description="Name of the cached material"
    ) #type: ignore

class BentNormalDebugPropertyGroup(bpy.types.PropertyGroup):
    enabled: bpy.props.BoolProperty(
        name="Enabled",
        default=False,
        description="Enabled"
    ) #type: ignore

    cached_materials: bpy.props.CollectionProperty(
        type=CachedMaterialPropertyGroup,
        name="Cached Materials",
        description="Cached materials"
    ) #type: ignore

class BentNormalPropertyGroup(bpy.types.PropertyGroup):
    # 参与碰撞检查的集合名称
    def get_collection_items(self, context):
        return [(col.name, col.name, "") for col in bpy.data.collections]
        
    collection_name: bpy.props.EnumProperty(
        name="Collection for collision",
        description="The collection containing the collision meshes",
        items=get_collection_items
    ) #type: ignore

    # 反转法线
    reverse_normal: bpy.props.BoolProperty(
        name="Reverse Normal",
        default=False,
        description="Reverse the normal direction"
    ) #type: ignore

    # 采样角度
    sample_angle: bpy.props.FloatProperty(
        name="Sample Angle",
        default=100.0,
        min=90.0,
        max=180.0,
        precision=0,
        step=1.0,
        description="The angle in degrees between each sample ray"
    ) #type: ignore

    # 采样次数
    sample_count: bpy.props.IntProperty(
        name="Sample Count",
        default=128,
        min=64,
        max=1024,
        description="The number of sample rays per vertex"
    ) #type: ignore

    # 射线偏移
    ray_offset: bpy.props.FloatProperty(
        name="Ray Offset",
        default=0.001,
        min=0.0001,
        max=0.1,
        precision=4,
        step=0.001,
        description="The offset of the sample ray from the surface"
    ) #type: ignore

    # 射线最大距离
    max_distance: bpy.props.FloatProperty(
        name="Max Distance",
        default=0.8,
        min=0.0,
        max=5.0,
        precision=1,
        step=0.1,
        description="The maximum distance to sample from the surface"
    ) #type: ignore

class BentNormalBakerOperator(bpy.types.Operator):
    bl_label = "Bake BentNormal"
    bl_idname = "bent_normal_baker.bake"
    bl_description = """将顶点周围各方向的遮挡信息积分到一阶球谐系数"""

    option: bpy.props.EnumProperty(
        name="Option",
        items=[("Bake", "Bake", ""), ("Apply", "Apply", ""), ("Clear", "Clear", "")],
        default="Bake",
        description="Bake or Clear"
    ) #type: ignore

    def bake(self, context, active_object):
        with bpy.context.temp_override(mode='OBJECT'):
            node = get_node("ProcessBentNormal")
            for mod in active_object.modifiers:
                if mod.type == 'NODES' and mod.node_group == node:
                    active_object.modifiers.remove(mod)
                    break

            prop = context.scene.bent_normal_baker
            args = {
                "reverse_normal": prop.reverse_normal,
                "sample_angle": prop.sample_angle,
                "sample_count": prop.sample_count,
                "ray_offset": prop.ray_offset,
                "max_distance": prop.max_distance,
                "collection_name": prop.collection_name,
            }

            baker = Baker(context, active_object, args)
            baker.bake()

            node = get_node("ProcessBentNormal")
            add_node_modifier(active_object, node)

            prop_debug = active_object.bent_normal_debug
            prop_debug.enabled = True
            
            prop_debug.cached_materials.clear()
            for mat in active_object.data.materials:
                if mat:
                    item = prop_debug.cached_materials.add()
                    item.name = mat.name

            # 将Debug Material应用到物体
            debug_material = get_material("Baker_DebugMaterial")
            replace_material(active_object, debug_material)

            # 将HDR图像转换为SH2系数
            # 给Debug Material的Irradiance使用
            if not bake_irradiance():
                self.report({'ERROR'}, "Bake irradiance failed")

    def apply(self, context, active_object):
        node = get_node("ProcessBentNormal")
        for mod in active_object.modifiers:
            if mod.type == 'NODES' and mod.node_group == node:
                with context.temp_override(active_object=active_object):
                    bpy.ops.object.modifier_apply(
                        modifier=mod.name,
                        single_user=True
                    )
                break

        self.clear(context, active_object)

    def clear(self, context, active_object):
        prop_debug = active_object.bent_normal_debug
        prop_debug.enabled = False

        # 从缓存中恢复材质
        active_object.data.materials.clear()
        for mat_item in prop_debug.cached_materials:
            material = get_material(mat_item.name)
            active_object.data.materials.append(material)
        prop_debug.cached_materials.clear()

        # 删除baked attributes
        clear_baked_attributes(active_object)

        # 删除geometry node
        node = get_node("ProcessBentNormal")
        for mod in active_object.modifiers:
            if mod.type == 'NODES' and mod.node_group == node:
                active_object.modifiers.remove(mod)

    def execute(self, context):
        active_object = bpy.context.active_object
        if active_object is None:
            self.report({'ERROR'}, "No active object selected")
            return {'CANCELLED'}
        
        if self.option == "Bake":
            self.bake(context, active_object)
        elif self.option == "Apply":
            self.apply(context, active_object)
        elif self.option == "Clear":
            self.clear(context, active_object)
        
        return {'FINISHED'}

class BentNormalBakerPanel(bpy.types.Panel):
    bl_label = "BentNormal Baker"
    bl_idname = "VIEW3D_PT_bent_normal_baker"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Tool"  

    def draw(self, context):
        prop = context.scene.bent_normal_baker

        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False  # 去掉属性的装饰器

        col = layout.column(align=True)
        col.prop(prop, "collection_name")
        # col.prop(prop, "reverse_normal")
        col.prop(prop, "sample_count")
        col.prop(prop, "sample_angle", slider=True)
        col.prop(prop, "ray_offset")
        col.prop(prop, "max_distance")

        col = layout.column(align=True)
        col.scale_y = 1.2  
        row = col.row(align=True)
        row.operator("bent_normal_baker.bake", text="Bake BentNormal").option = "Bake"

        active_object = bpy.context.active_object
        if active_object is not None and active_object.bent_normal_debug.enabled:
            row.operator("bent_normal_baker.bake", text="", icon="CHECKMARK").option = "Apply"
            row.operator("bent_normal_baker.bake", text="", icon="TRASH").option = "Clear"

class_list = [
    CachedMaterialPropertyGroup,    
    BentNormalBakerPanel,
    BentNormalBakerOperator,
    BentNormalPropertyGroup,
    BentNormalDebugPropertyGroup,
]

def register():
    for cls in class_list:
        bpy.utils.register_class(cls)
    bpy.types.Scene.bent_normal_baker = bpy.props.PointerProperty(type=BentNormalPropertyGroup)
    bpy.types.Object.bent_normal_debug = bpy.props.PointerProperty(type=BentNormalDebugPropertyGroup)

def unregister():
    for cls in class_list:
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.bent_normal_baker
    del bpy.types.Object.bent_normal_debug

if __name__ == "__main__":
    register()
