import argparse
import yaml
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import matplotlib.animation as manimation
from gui.grid_image_visualizer import GridImageVisualizer
import envs
import policy
import utils


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('env_fname', type=str, help='config file with environment arguments')
    parser.add_argument('pol_fname', type=str, help='config file with policy arguments')
    parser.add_argument('--output_dir', '-o', type=str, default=None)
    parser.add_argument('--num_trajs', '-n', type=int, default=10, metavar='N', help='total number of data points is N*T')
    parser.add_argument('--num_steps', '-t', type=int, default=10, metavar='T', help='number of time steps per trajectory')
    parser.add_argument('--visualize', '-v', type=int, default=None)
    parser.add_argument('--record_file', '-r', type=str, default=None)
    args = parser.parse_args()

    with open(args.env_fname) as yaml_string:
        env_config = yaml.load(yaml_string)
        if issubclass(env_config['class'], envs.RosEnv):
            import rospy
            rospy.init_node("generate_data")
        env = utils.from_config(env_config)

    with open(args.pol_fname) as yaml_string:
        policy_config = yaml.load(yaml_string)
        replace_config = {'env': env,
                          'action_space': env.action_space,
                          'state_space': env.state_space}
        try:
            replace_config['target_env'] = env.car_env
        except AttributeError:
            pass
        # TODO: better way to populate config with existing instances
        pol = utils.from_config(policy_config, replace_config=replace_config)
        assert len(pol.policies) == 2
        target_pol, random_pol = pol.policies
        assert isinstance(target_pol, policy.TargetPolicy)
        assert isinstance(random_pol, policy.RandomPolicy)

    if args.output_dir:
        container = utils.container.ImageDataContainer(args.output_dir, 'x')
        container.reserve(env.sensor_names + ['state'], (args.num_trajs, args.num_steps + 1))
        # save errors if they are available (e.g. env defines get_errors())
        try:
            error_names = list(env.get_errors(target_pol.get_target_state()).keys())
        except AttributeError:
            error_names = []
        container.reserve(['action', 'state_diff'] + error_names, (args.num_trajs, args.num_steps))
        container.add_info(environment_config=env.get_config())
        container.add_info(policy_config=pol.get_config())
    else:
        container = None

    if args.record_file and not args.visualize:
        args.visualize = 1
    if args.visualize:
        fig = plt.figure(figsize=(16, 12), frameon=False, tight_layout=True)
        gs = gridspec.GridSpec(1, 1)
        image_visualizer = GridImageVisualizer(fig, gs[0], len(env.sensor_names))
        plt.show(block=False)

        if args.record_file:
            FFMpegWriter = manimation.writers['ffmpeg']
            writer = FFMpegWriter(fps=1.0 / env.dt)
            writer.setup(fig, args.record_file, fig.dpi)

    done = False
    for traj_iter in range(args.num_trajs):
        print('traj_iter', traj_iter)
        try:
            prev_state = None
            state = pol.reset()
            env.reset(state)
            for step_iter in range(args.num_steps):
                state, obs = env.get_state_and_observe()
                action = pol.act(obs)
                env.step(action)  # action is updated in-place if needed
                if container:
                    if step_iter > 0:
                        container.add_datum(traj_iter, step_iter - 1, state_diff=state - prev_state)
                    try:
                        errors = env.get_errors(target_pol.get_target_state())
                    except AttributeError:
                        errors = dict()
                    container.add_datum(traj_iter, step_iter, state=state, action=action,
                                        **dict(list(errors.items()) + list(zip(env.sensor_names, obs))))
                    prev_state = state
                    if step_iter == (args.num_steps-1):
                        next_state, next_obs = env.get_state_and_observe()
                        container.add_datum(traj_iter, step_iter, state_diff=next_state - state)
                        container.add_datum(traj_iter, step_iter + 1, state=next_state,
                                            **dict(zip(env.sensor_names, next_obs)))
                if args.visualize:
                    env.render()
                    try:
                        image_visualizer.update(obs)
                        if args.record_file:
                            writer.grab_frame()
                    except:
                        done = True
                    if done:
                        break
            if done:
                break
        except KeyboardInterrupt:
            break
    env.close()
    if args.record_file:
        writer.finish()
    if container:
        container.close()


if __name__ == "__main__":
    main()
