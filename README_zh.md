
<p align="left">
    中文</a>&nbsp ｜ &nbsp<a href="README.md">English</a>&nbsp
</p>

# BentNormal Baker

![BentNormal Baker](./assets/image/BentNormalBaker.jpg)

Blender插件, 用于烘焙弯曲法线(BentNormal)等三维自遮挡数据到模型的顶点颜色。

## 功能

- 将每个顶点周围的遮挡信息作为一阶球谐函数的4个系数烘焙到顶点颜色中
- 包含演示着色器，展示如何应用球谐函数实现BentNormal和AO
- 提供基于烘焙结果的调试材质，用于可视化适合移动游戏开发的廉价的SSS、反射遮挡和接触阴影等效果
- 多线程烘焙过程，优化CPU利用率
- 可自定义参数：采样数量、射线偏移、最大距离
- 自动切线空间计算

## 安装

1. 将插件文件夹复制到Blender的插件目录
2. 在Blender中启用插件

## 使用方法

1. 在3D视图的侧边栏的Tool面板的下方找到`BentNormal Baker`面板

> <p align="left"><img src="./assets/image/panel.jpg" alt="Bent Normal Baker" style="width: 330px; height: auto;"></p>

2. 参数：
   - Collection for collision: 包含碰撞物体的集合名称
   - Sample Count: 每个顶点的采样数量
   - Sample Angle: 采样角度
   - Ray Offset: 射线起点偏移
   - Max Distance: 射线最大距离

3. 选择要烘焙的网格物体，并将所有碰撞物体添加到指定的集合中

4. 确保所有碰撞物体的变换在世界原点（位置0，旋转0，缩放1）。使用<kbd>Ctrl</kbd>(<kbd>Cmd</kbd>)+<kbd>A</kbd> → `Apply All Transforms`

5. 点击<kbd>Bake</kbd>开始烘焙

6. 烘焙完成后，将生成以下网格属性：
   - `SH0`：0阶球谐函数系数（FLOAT）
   - `SH1`：1阶球谐函数系数（FLOAT_VECTOR）
   - `Tangent`：切线向量（FLOAT_VECTOR）

7. 将自动添加一个`ProcessBentNormal`几何节点修改器：
   - 在修改器面板中调整弯曲法线和AO模糊
   - 可选择将结果存储在TBN空间中

> <p align="left"><img src="./assets/image/modifier.jpg" alt="modifier" style="width: 330px; height: auto;"></p>

8. 将自动添加一个`Baker_DebugMaterial`：
   - 使用<kbd>Shift</kbd>+<kbd>Ctrl</kbd>(<kbd>Cmd</kbd>)+<kbd>点击</kbd>`Debug SSS Shading`节点切换调试信息
   - 使用`Debug SSS Shading`节点中的`Effect Toggle`来快速预览是否启用遮挡数据的效果
   - 在`Debug SSS Shading`节点中按<kbd>Tab</kbd>查看着色器实现细节

9. 对于蒙皮网格，在几何节点修改器中启用`TBN space`。静态网格请关闭该选项

10. 点击<kbd>✓</kbd>按钮，该操作会将结果保存到`ProcessBentNormal`几何节点修改器设置的顶点颜色属性并清理中间数据

> <p align="left"><img src="./assets/image/apply.jpg" alt="apply" style="width: 300px; height: auto;"></p>

## 注意事项

- 开发于Blender 4.3，4.2以下版本可能存在兼容性问题
- 烘焙前确保所有碰撞物体具有正确的Transform变换
- 所有参与遮挡的物体必须在指定集合中
- 更高的`Sample Count`提高精度但会增加计算时间
- `Ray Offset`过小可能导致自相交，过大可能导致漏光
- 根据场景比例调整`Max Distance`
- 如果在`TBN space`下预览的结果在UV接缝处有明显的色差。可以新建一个材质，再新建一个`Tangent`节点并设置参数为`UV Map`，一边预览该节点的效果一边调整模型的UV, 直到UV接缝处没有明显的色差为止, 或者也可以试着改变UV的接缝位置。

## 开发

- 使用Python 3.10+编写
- 使用NumPy进行高效数值计算
- 利用Blender的BVH树进行射线投射
- 支持多线程并行计算
