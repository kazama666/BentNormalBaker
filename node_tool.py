import os
import bpy

# 将相对路径转换为绝对路径
NODE_FILE_PATH = os.path.join(os.path.dirname(__file__), "assets/node/nodes.blend")

def get_node(node_name):
    """
    从指定的blend文件中获取节点
    
    Args:
        node_name (str): 节点名
    """
    # 首先检查节点是否已经在当前文件中存在
    if node_name in bpy.data.node_groups:
        return bpy.data.node_groups[node_name]
        
    # 如果节点不存在，则从外部文件获取
    with bpy.data.libraries.load(NODE_FILE_PATH, link=True) as (data_from, data_to):
        if node_name in data_from.node_groups:
            data_to.node_groups = [node_name]
        else:
            return None
            
    return bpy.data.node_groups[node_name]

def get_material(material_name):
    """
    从指定的blend文件中获取材质
    
    Args:
        material_name (str): 材质名
    """
    # 首先检查材质是否已经在当前文件中存在
    if material_name in bpy.data.materials:
        return bpy.data.materials[material_name]
    
    # 如果材质不存在，则从外部文件获取  
    with bpy.data.libraries.load(NODE_FILE_PATH, link=False) as (data_from, data_to):
        if material_name in data_from.materials:
            data_to.materials = [material_name]
        else:
            return None
            
    return bpy.data.materials[material_name]
    

def add_node_modifier(obj, node):
    """
    给对象添加几何节点修改器并指定节点
    
    Args:
        obj (bpy.types.Object): 要添加修改器的对象
        node (bpy.types.NodeGroup): 要使用的节点组
    """
    for mod in obj.modifiers:
        if mod.type == 'NODES' and mod.node_group == node:
            return
        
    modifier = obj.modifiers.new(name=node.name, type='NODES')
    modifier.node_group = node

def replace_material(obj, material):
    """
    给对象设置材质
    """
    obj.data.materials.clear()
    obj.data.materials.append(material)


