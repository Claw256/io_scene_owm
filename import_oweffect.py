import os

from . import read_oweffect
from . import import_owmdl
from . import bpyhelper
from . import owm_types
from . import import_owmdl
from . import read_owmdl
from mathutils import *
import math
import bpy, bpy_extras, mathutils
import os

def get_object(obj=None): # return => is_entity, object, model_index
    if obj is None:
        obj = bpy.context.object
    if 'owm.entity.guid' in obj:
        return True, obj, obj['owm.entity.model']
    if 'owm.model.guid' in obj:
        model = obj['owm.model.guid']
        if obj.parent is not None and 'owm.entity.model' in obj.parent:
            return True, obj.parent, model
        else:
            return False, obj, model
    elif obj.parent is not None:
        return get_object(obj.parent)

    return False, None, 0

def get_skeleton(is_entity, obj, model):
    model_container = None
    if is_entity:
        for o in obj.children:
            if 'owm.model.guid' in o:
                model_container = o
    else:
        model_container = obj

    skeleton = None
    for o in model_container.children:
        if 'owm.skeleton.model' in o:
            if o['owm.skeleton.model'] == model:
                return o


def get_meshes(skeleton):
    meshes = list()
    for o in skeleton.children:
        if 'owm.mesh.name' in o:
            meshes.append(o)
    return meshes


def create_refpose(model_path):
    model_data = read_owmdl.read(model_path)
    arm = import_owmdl.import_refpose_armature(False, model_data)

    att = bpy.data.objects.new('Hardpoints', None)
    att.parent = arm
    att.hide = att.hide_render = True
    att['owm.hardpoint_container'] = True
    bpyhelper.scene_link(att)

    e_dict = {}
    for emp in model_data.empties:
        bpy.ops.object.empty_add(type='CIRCLE', radius=0.05 )
        empty = bpy.context.active_object
        empty.parent = att
        empty.name = emp.name
        empty.show_x_ray = True
        empty.location = import_owmdl.xzy(emp.position)
        empty.rotation_euler = import_owmdl.wxzy(emp.rotation).to_euler('XYZ')
        empty['owm.hardpoint.bone'] = emp.hardpoint
        bpyhelper.select_obj(empty, True)
        if len(emp.hardpoint) > 0:
            copy_location = empty.constraints.new("COPY_LOCATION")
            copy_location.name = "Hardpoint Location"
            copy_location.target = arm
            copy_location.subtarget = emp.hardpoint
            # copy_location.use_offset = True

            copy_rotation = empty.constraints.new("COPY_ROTATION")
            copy_rotation.name = "Hardpoint Rotation"
            copy_rotation.target = arm
            copy_rotation.subtarget = emp.hardpoint

            # copy_rotation.use_offset = True
            
        e_dict[empty.name] = empty
    return arm, e_dict, att


def attach(par, obj):
    copy_location = obj.constraints.new("COPY_LOCATION")
    copy_location.name = "Hardpoint Location"
    copy_location.target = par

    copy_rotation = obj.constraints.new("COPY_ROTATION")
    copy_rotation.name = "Hardpoint Rotation"
    copy_rotation.target = par

    copy_scale = obj.constraints.new("COPY_SCALE")
    copy_scale.name = "Hardpoint Scale"
    copy_scale.target = par

    return copy_location, copy_rotation, copy_scale

def delete(obj):
    bpy.ops.object.select_all(action='DESELECT')
    obj.select = True
    bpy.ops.object.delete()


def process(settings, data, pool, parent, target_framerate, hardpoints, variables):
    if type(data) == owm_types.OWAnimFile:
        if target_framerate is None:
            target_framerate = int(data.header.fps)
        
        is_entity, obj, model = get_object()
        
        this_obj = bpy.data.objects.new('Animation {}'.format(os.path.splitext(os.path.basename(data.anim_path))[0]), None)
        this_obj.hide = this_obj.hide_render = True
        bpyhelper.scene_link(this_obj)
        if parent is None:
            parent = this_obj
            this_obj.parent = obj
        else:
            this_obj.parent = parent
        
        skeleton = get_skeleton(is_entity, obj, model)
        
        bpy.ops.object.select_all(action='DESELECT')

        new_skeleton, hardpoints, hp_container = create_refpose(os.path.join(pool, data.model_path))
        new_skeleton.parent = this_obj

        for mesh in get_meshes(skeleton):
            if 'OWM Skeleton' in mesh.modifiers:
                mod = mesh.modifiers['OWM Skeleton']
                mod.object = new_skeleton

        new_skeleton.rotation_euler = (math.radians(90), 0, 0)
        
        directory, file = os.path.split(data.anim_path)
        bpy.ops.object.select_all(action='DESELECT')
        bpy.context.scene.objects.active = new_skeleton
        bpy.ops.import_scene.seanim(filepath=os.path.join(pool, directory) + "\\", files=[{'name': file}])

        if target_framerate != int(data.header.fps):
            scale = target_framerate / int(data.header.fps)
            bpy.context.scene.frame_end *= scale
            bpy.ops.object.mode_set(mode='OBJECT', toggle=False)
            bpy.ops.object.select_all(action='DESELECT')
            bpy.context.scene.objects.active = new_skeleton

            for f in new_skeleton.animation_data.action.fcurves:
                for kp in f.keyframe_points:
                    kp.co[0] *= scale

        bpy.context.scene.render.fps = target_framerate

        if is_entity:
            for c in obj.children:
                if 'owm.entity.child.var' in c:
                    var = c['owm.entity.child.var']
                    ent_obj = bpy.data.objects.new('EffectEntityWrapper {}'.format(var), None)
                    ent_obj.hide = this_obj.hide_render = True
                    ent_obj.parent = hardpoints[c['owm.entity.child.hardpoint']]
                    ent_obj.parent['owm.effect.hardpoint.used'] = True
                    ent_obj['owm.entity.model'] = c['owm.entity.model']
                    bpyhelper.scene_link(ent_obj)
                    variables[var] = 'entity_child', ent_obj, c

                    if 'ChildEntity Location' in c.constraints:
                        c.constraints['ChildEntity Location'].target = ent_obj

                    if 'ChildEntity Rotation' in c.constraints:
                        c.constraints['ChildEntity Rotation'].target = ent_obj

                    if 'ChildEntity Scale' in c.constraints:
                        c.constraints['ChildEntity Scale'].target = ent_obj
        
        process(settings, data.data, pool, this_obj, target_framerate, hardpoints, variables)

        if bpy.context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT', toggle=False)
        for hp_name,hp in hardpoints.items():
            if 'owm.effect.hardpoint.used' not in hp:
                delete(hp)
            else:
                del hp['owm.effect.hardpoint.used']

        if len(hp_container.children) == 0:
            delete(hp_container)

        return this_obj, new_skeleton

    if type(data) == owm_types.OWEffectData:
        obj = bpy.data.objects.new('Effect {}'.format(data.guid), None)
        obj.parent = parent
        obj.hide = obj.hide_render = True
        bpyhelper.scene_link(obj)

        for dmce in data.dmces:
            if not settings.import_DMCE:
                continue
            end_frame = bpy.context.scene.frame_end
            mutate = settings.settings.mutate(os.path.join(pool, dmce.model_path))
            mutate.importEmpties = False
            dmce_model = import_owmdl.read(mutate) # rootObject, armature, meshes, empties, data
            dmce_model[0].parent = obj
            dmce_skele = None
            if dmce.anim_path != "null":
                mutate2 = settings.mutate(os.path.join(pool, dmce.anim_path))
                mutate2.force_fps = True
                mutate2.target_fps = target_framerate
                dmce_obj, dmce_skele = read(mutate2, obj)

                for f in dmce_skele.animation_data.action.fcurves:
                    for kp in f.keyframe_points:
                        kp.co[0] += int(target_framerate * dmce.time.start)

                delete(dmce_model[0])
                delete(dmce_model[1])

                for mesh in dmce_model[2]:
                    mesh.parent = dmce_skele
                    if dmce.time.hardpoint != "null":
                        attach(hardpoints[dmce.time.hardpoint], mesh)                

            bpy.context.scene.frame_end = end_frame
            if dmce.time.hardpoint != "null":
                if dmce_skele is not None:
                    attach(hardpoints[dmce.time.hardpoint], dmce_skele)
                    dmce_model[0].parent = dmce_obj
                else:
                    dmce_model[1].rotation_euler = (0, 0, 0)
                attach(hardpoints[dmce.time.hardpoint],dmce_model[0])
                hardpoints[dmce.time.hardpoint]['owm.effect.hardpoint.used'] = True
       
        show_ents = []
                    
        for cece in data.ceces:
            if not settings.import_CECE:
                continue
            if cece.var_index not in variables:
                print("[import_effect]: Could not find CECE entity {} (animation={})".format(cece.var_index, cece.path))
                continue
            else:
                var_id, cece_container, cece_entity = variables[cece.var_index]
            if cece.action == owm_types.CECEAction.Show:
                show_ents.append(cece.var_index)
            if cece.action == owm_types.CECEAction.PlayAnimation:
                mutate = settings.mutate(os.path.join(pool, "Models\\{0:012x}.00C\\{1}".format(cece_entity['owm.entity.model'],cece.path)))
                mutate.force_fps = True
                mutate.target_fps = target_framerate

                if not os.path.exists(mutate.filename):
                    print("[import_effect]: Missing CECE \"Models\\{0:012x}.00C\\{1}\"".format(cece_entity['owm.entity.model'],cece.path))
                    continue

                if bpy.context.object.mode != 'OBJECT':
                    bpy.ops.object.mode_set(mode='OBJECT', toggle=False)
                bpy.ops.object.select_all(action='DESELECT')
                bpy.context.scene.objects.active = cece_entity

                anim_container, anim_skele = read(mutate, obj)
                anim_skele.rotation_euler = (0, 0, 0)
                attach(cece_container, anim_container)

                for f in anim_skele.animation_data.action.fcurves:
                    for kp in f.keyframe_points:
                        kp.co[0] += int(target_framerate * cece.time.start)
        
        for var, var_data in variables.items():
            if not settings.import_CECE:
                continue
            if var_data[0] != "entity_child":
                continue
            var_id, cece_container, cece_entity = var_data
            if cece_entity['owm.entity.child.var'] not in show_ents:
                # cycles dies at init time when this is used. todo: why
                cece_container.scale = (0, 0, 0)
                # this is using keyframes
##                act = bpy.context.scene.objects.active
##                if bpy.context.object.mode != 'OBJECT':
##                    bpy.ops.object.mode_set(mode='OBJECT', toggle=False)
##                bpy.ops.object.select_all(action='DESELECT')
##                bpy.context.scene.objects.active = cece_container
##                cece_container.select = True
##                
##                frame = 0
##                end_frame = 0 # not used
##                
##                bpy.context.scene.frame_set(frame)
##                cece_container.scale = (0, 0, 0)
##                bpy.ops.anim.keyframe_insert_menu(type='Scaling')
##                bpy.context.scene.frame_set(1)
##                cece_container.select = False
##                bpy.context.scene.objects.active = act
                
        
def read(settings, existing_parent=None):
    root, file = os.path.split(settings.filename)

    data = read_oweffect.read(settings.filename)
    if data is None: return None

    if type(data) == owm_types.OWAnimFile:
        pool = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(settings.filename)))))
        t = None
        if settings.force_fps:
            t = settings.target_fps
        ret = process(settings, data, pool, existing_parent, t, None, {})
        
        if existing_parent is None and settings.create_camera:
            bpy.ops.object.add(type="CAMERA")
            cam = bpy.context.active_object
            cam.name = "AnimationCamera {}".format(os.path.splitext(os.path.basename(data.anim_path))[0])
            loc, rot, scale = attach(ret[1], cam)
            loc.subtarget = "bone_007D"
            rot.subtarget = "bone_007D"
            rot.use_offset = True
            cam.rotation_euler = (0, math.radians(180), 0)
            cam.parent = ret[0]
            # this value looks close?
            cam.data.lens = 24
        return ret